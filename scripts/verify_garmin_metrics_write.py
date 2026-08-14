#!/usr/bin/env python3
"""Ad-hoc verification of the garmin PGRST204 fix (commit 9bf84fc, 2026-08-14).

NOT a test suite — the project has no pytest coverage for the garmin path.
This is a focused, re-runnable check of the changed behaviour in:
    db/client.py          (_coerce_garmin_row whitelist)
    run_garmin.py         (pop time-series keys BEFORE aggregate upsert)
    run_garmin_today.py   (same)

It hits the LIVE Garmin API and the LIVE Supabase project — it is a smoke
check, not a hermetic unit test. Re-run it after any edit to the garmin
provider, the runners, or garmin_metrics' schema.

Falsification-tested: reverting either half of the fix makes it report 12
FAILs including the original PGRST204 / 400 / rc!=0 fingerprint, so a green
run is meaningful rather than vacuous.

Usage:
    cd /root/morning_brief_v2 && .venv/bin/python scripts/verify_garmin_metrics_write.py

Exit 0 = every check passed. Exit 1 = at least one FAIL (fix incomplete).
"""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT = "/root/morning_brief_v2"
VENV_PY = f"{PROJECT}/.venv/bin/python"

# Aggregate fields the brief needs. 11/11 is the acceptance bar (§80).
AGG_FIELDS = [
    "body_battery", "hrv", "rhr", "sleep_duration_min", "deep_sleep_pct",
    "sleep_score", "training_readiness", "spo2", "stress",
    "total_steps", "distance_km",
]

# Keys the provider emits for garmin_sleep_minutes (migration 009) that must
# NEVER reach the garmin_metrics payload.
TIMESERIES_KEYS = ["sleep_minute_rows", "sleep_minute_count"]

_results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: object, detail: str = "") -> None:
    _results.append((name, bool(cond), str(detail)))


def load_env() -> None:
    """Load .env exactly like the cron wrapper does (set -a && . ./.env)."""
    r = subprocess.run(
        ["bash", "-c", "set -a && . ./.env && set +a && env"],
        capture_output=True, text=True, cwd=PROJECT, timeout=30,
    )
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ[k] = v


def run_project(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, cwd=PROJECT, timeout=timeout,
        env=os.environ.copy(),
    )


# ---------------------------------------------------------------- section 1
def check_coercion_unit() -> None:
    """_coerce_garmin_row: drops out-of-schema keys, keeps + casts valid ones."""
    from db.client import _coerce_garmin_row

    payload = {
        # valid, float-typed as Garmin really returns them
        "body_battery": 73.0, "hrv": 61.0, "rhr": 51.0,
        "sleep_duration_min": 390.0, "sleep_score": 73.0,
        "training_readiness": 79.0, "stress": 17.0, "total_steps": 133.0,
        "resting_kcal": 1600.0, "active_kcal": 320.0,
        "deep_sleep_pct": 14.63, "spo2": 93.0, "skin_temp": -0.4,
        "distance_km": 0.111,
        # the two that caused PGRST204
        "sleep_minute_count": 591,
        "sleep_minute_rows": [{"minute_ts": "2026-08-14T01:00:00Z"}] * 3,
        # any future out-of-schema field must also be survivable
        "some_future_field": "boom",
        # None must be preserved (legit "watch not worn")
        "sleep_score_none_probe": None,
    }
    out = _coerce_garmin_row(payload)

    for k in TIMESERIES_KEYS:
        chk(f"_coerce drops {k}", k not in out, f"out keys={sorted(out)}")
    chk("_coerce drops unknown future field",
        "some_future_field" not in out, f"out keys={sorted(out)}")
    chk("_coerce drops unknown None-valued field",
        "sleep_score_none_probe" not in out, "")

    # valid aggregates survive with correct types
    chk("int cast: body_battery 73.0 -> int 73",
        out.get("body_battery") == 73 and isinstance(out["body_battery"], int),
        f"got {out.get('body_battery')!r}")
    chk("int cast: total_steps 133.0 -> int 133",
        out.get("total_steps") == 133 and isinstance(out["total_steps"], int),
        f"got {out.get('total_steps')!r}")
    chk("numeric round: deep_sleep_pct 14.63",
        out.get("deep_sleep_pct") == 14.63, f"got {out.get('deep_sleep_pct')!r}")
    chk("numeric round: distance_km 0.111 -> 0.11",
        out.get("distance_km") == 0.11, f"got {out.get('distance_km')!r}")
    chk("negative numeric preserved: skin_temp -0.4",
        out.get("skin_temp") == -0.4, f"got {out.get('skin_temp')!r}")
    chk("all 14 valid aggregate keys survive",
        len(out) == 14, f"kept {len(out)}: {sorted(out)}")

    # None on a KNOWN column must stay None (not dropped, not coerced to 0)
    out_none = _coerce_garmin_row({"sleep_score": None, "hrv": 60.0})
    chk("None on known column preserved as None",
        "sleep_score" in out_none and out_none["sleep_score"] is None,
        f"got {out_none!r}")


def check_whitelist_matches_db() -> None:
    """Whitelist must equal the real DB columns (minus autogenerated id)."""
    from db.client import _GARMIN_KNOWN_COLS, get_client

    sb = get_client()
    r = sb.table("garmin_metrics").select("*").order("date", desc=True).limit(1).execute()
    chk("garmin_metrics has at least one row to introspect", bool(r.data), "")
    if not r.data:
        return
    db_cols = set(r.data[0].keys()) - {"id"}
    missing_from_whitelist = db_cols - set(_GARMIN_KNOWN_COLS)
    extra_in_whitelist = set(_GARMIN_KNOWN_COLS) - db_cols

    chk("no real DB column is missing from whitelist (would silently drop data)",
        not missing_from_whitelist, f"missing={sorted(missing_from_whitelist)}")
    chk("no phantom column in whitelist (would re-trigger PGRST204)",
        not extra_in_whitelist, f"extra={sorted(extra_in_whitelist)}")


# ---------------------------------------------------------------- section 2
def check_runner_source_order() -> None:
    """Static AST check: both runners pop time-series keys BEFORE the upsert.

    This is the actual regression: popping after the upsert is what sent the
    out-of-schema key to PostgREST. Guard against a future re-reorder.
    """
    for fname in ("run_garmin.py", "run_garmin_today.py"):
        src = Path(PROJECT, fname).read_text(encoding="utf-8")
        tree = ast.parse(src)

        pop_lines: dict[str, int] = {}
        upsert_line: int | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # metrics.pop("sleep_minute_rows" | "sleep_minute_count", ...)
            if (isinstance(f, ast.Attribute) and f.attr == "pop"
                    and isinstance(f.value, ast.Name) and f.value.id == "metrics"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                key = node.args[0].value
                if key in TIMESERIES_KEYS:
                    pop_lines.setdefault(key, node.lineno)
            if isinstance(f, ast.Name) and f.id == "upsert_garmin_metrics":
                if upsert_line is None:
                    upsert_line = node.lineno

        chk(f"{fname}: upsert_garmin_metrics call found",
            upsert_line is not None, f"line={upsert_line}")
        for key in TIMESERIES_KEYS:
            chk(f"{fname}: pops {key}", key in pop_lines,
                f"pop_lines={pop_lines}")
            if key in pop_lines and upsert_line is not None:
                chk(f"{fname}: pop({key}) is BEFORE upsert "
                    f"(L{pop_lines[key]} < L{upsert_line})",
                    pop_lines[key] < upsert_line,
                    f"pop@{pop_lines[key]} upsert@{upsert_line}")


def check_provider_still_emits_timeseries() -> None:
    """Proves the risk is LIVE, not hypothetical: provider does return the keys.

    If this FAILs, the provider changed and the whitelist is guarding nothing —
    still correct, but the regression fingerprint in §80 needs revisiting.
    """
    import asyncio

    from providers.garmin import GarminProvider

    async def _go():
        return await GarminProvider().fetch(target_date=date.today() - timedelta(days=1))

    res = asyncio.run(_go())
    data = getattr(res, "data", None) or {}
    chk("provider fetch returned data", bool(data),
        f"status={getattr(res, 'status', None)}")
    present = [k for k in TIMESERIES_KEYS if k in data]
    chk("provider STILL emits sleep_minute_* (whitelist is load-bearing)",
        bool(present), f"present={present}")


# ---------------------------------------------------------------- section 3
def check_runners_execute() -> None:
    """Real execution on real disk — no mocks (§64)."""
    yday = (date.today() - timedelta(days=1)).isoformat()

    # NOTE: the runners configure logging.basicConfig(), which writes to
    # STDERR, not stdout. Always assert against the combined streams —
    # checking r.stdout alone gives a false FAIL (hit while writing this).
    r1 = run_project([VENV_PY, "run_garmin.py", "--date", yday])
    out1 = (r1.stdout or "") + (r1.stderr or "")
    chk(f"run_garmin.py --date {yday} exits 0", r1.returncode == 0,
        f"rc={r1.returncode} tail={out1[-200:]!r}")
    chk("run_garmin.py: garmin_metrics POST is 2xx (no 400)",
        "garmin_metrics" in out1 and "400 Bad Request" not in out1,
        "found '400 Bad Request'" if "400 Bad Request" in out1
        else ("no garmin_metrics line at all" if "garmin_metrics" not in out1
              else "clean"))
    chk("run_garmin.py: no PGRST204 in output", "PGRST204" not in out1, "")

    r2 = run_project([VENV_PY, "run_garmin_today.py"])
    out2 = (r2.stdout or "") + (r2.stderr or "")
    chk("run_garmin_today.py exits 0", r2.returncode == 0,
        f"rc={r2.returncode} tail={out2[-200:]!r}")
    chk("run_garmin_today.py: garmin_metrics POST is 2xx (no 400)",
        "garmin_metrics" in out2 and "400 Bad Request" not in out2,
        "found '400 Bad Request'" if "400 Bad Request" in out2
        else ("no garmin_metrics line at all" if "garmin_metrics" not in out2
              else "clean"))
    chk("run_garmin_today.py: no PGRST204 in output", "PGRST204" not in out2, "")

    r3 = run_project(["./scripts/run_garmin_cron.sh"], timeout=300)
    chk("scripts/run_garmin_cron.sh (real cron path) exits 0",
        r3.returncode == 0, f"rc={r3.returncode}")
    log = Path(PROJECT, "logs/cron", f"garmin-{date.today().isoformat()}.log")
    chk("cron wrapper wrote today's log", log.exists(), str(log))
    if log.exists():
        txt = log.read_text(encoding="utf-8", errors="replace")
        # The daily log is APPEND-ONLY across every run of the day, so a stale
        # error from an earlier run (or from a deliberate revert experiment)
        # would give a false FAIL. Scope the assertions to the LAST run only,
        # i.e. everything after the final "[garmin] start" marker.
        marker = "[garmin] start"
        last_run = txt[txt.rfind(marker):] if marker in txt else txt
        chk("cron log: last run ends rc=0", "end rc=0" in last_run,
            last_run.strip()[-120:])
        chk("cron log: last run has no PGRST204", "PGRST204" not in last_run,
            "PGRST204 present in most recent run")
        chk("cron log: last run has no 400 Bad Request",
            "400 Bad Request" not in last_run, "")


def check_db_state() -> None:
    """11/11 aggregate fields for today and yesterday (§80 acceptance bar)."""
    from db.client import get_garmin_metrics

    for d in (date.today(), date.today() - timedelta(days=1)):
        row = get_garmin_metrics(d)
        chk(f"garmin_metrics row exists for {d}", row is not None, "")
        if row is None:
            continue
        filled = [f for f in AGG_FIELDS if row.get(f) is not None]
        chk(f"{d}: {len(filled)}/{len(AGG_FIELDS)} aggregate fields populated",
            len(filled) == len(AGG_FIELDS),
            f"empty={[f for f in AGG_FIELDS if row.get(f) is None]}")
        for k in TIMESERIES_KEYS:
            chk(f"{d}: DB row has no {k} leakage", k not in row, "")


def main() -> int:
    os.chdir(PROJECT)
    load_env()
    sys.path.insert(0, PROJECT)
    # surface the "dropped field(s)" WARNING so a silent filter is impossible
    logging.basicConfig(level=logging.WARNING,
                        format="    [log] %(levelname)s %(message)s")

    print("== 1. unit: _coerce_garmin_row + whitelist vs real schema ==")
    check_coercion_unit()
    check_whitelist_matches_db()

    print("== 2. static: runner call order + live provider payload ==")
    check_runner_source_order()
    check_provider_still_emits_timeseries()

    print("== 3. real execution + DB state ==")
    check_runners_execute()
    check_db_state()

    print()
    failed = 0
    for name, ok, detail in _results:
        if ok:
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name}\n        -> {detail}")
    total = len(_results)
    print(f"\n{total - failed}/{total} checks passed"
          f"{'' if not failed else f' — {failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
