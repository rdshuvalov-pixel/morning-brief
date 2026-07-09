#!/usr/bin/env python3
"""Pre/post-flight data check for morning brief render.

Verifies that all required providers have written fresh data for the target
date.

History (2026-07-05 bug B fix): this script used to gate the render — if any
required row was missing, rc=2 told render_with_retry.sh to retry. That made
the script block on data races (e.g. Garmin hrv not yet settled by 08:30)
and the retry loop blew the Hermes 120s budget. Now the render itself writes
all provider data, so this script's role is post-flight validation: warn
about missing fields, but never block the render.

Exit codes:
    0  All required data present
    1  Some data missing — caller decides what to do (do NOT auto-retry)
    3  Infrastructure broken (auth, schema, etc.)

Required fields (per provider, for `target`):
  garmin_metrics   : body_battery, rhr, sleep_duration_min, total_steps
                     (must exist in DB for target date)
                     hrv is OPTIONAL — Garmin API only returns HRV for
                     "settled" days (yesterday), not for "today" (live mode).
                     See references/garmin-data-race.md.
  briefs           : row must exist for target date
  weather_log      : at least 1 row for target date
  tasks            : at least 1 row for target date
  calendar_events  : 0+ rows OK (weekends/holidays may have none)
  food_log         : 0+ rows OK (may be empty if user hasn't logged)

Usage:
    ./.venv/bin/python preflight_check.py --date 2026-07-02
    ./.venv/bin/python preflight_check.py                # uses today (Lisbon)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/root/morning_brief_v2")

# Load .env BEFORE supabase reads it
for _line in Path("/root/morning_brief_v2/.env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#"):
        continue
    _k, _, _v = _line.partition("=")
    os.environ.setdefault(_k, _v)

try:
    from zoneinfo import ZoneInfo
    LISBON = ZoneInfo("Europe/Lisbon")
except Exception:
    LISBON = None


def lisbon_today() -> date:
    if LISBON:
        return datetime.now(LISBON).date()
    return datetime.utcnow().date()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pre-flight check for morning brief data")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Target date (default: Lisbon today)")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else lisbon_today())
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("preflight")

    target_str = target.isoformat()
    log.info("preflight check for %s", target_str)

    try:
        from db.client import get_client
    except Exception as e:
        log.error("cannot import db.client: %s", e)
        return 3
    try:
        sb = get_client()
    except Exception as e:
        log.error("Supabase client init failed: %s", e)
        return 3

    problems: list[str] = []

    def get_data(resp) -> list | dict | None:
        if resp is None:
            return None
        return getattr(resp, "data", None)

    # 1) Brief row must exist
    try:
        r = sb.table("briefs").select("id,collected_at").eq("date", target_str).maybe_single().execute()
        brief = get_data(r)
        if not brief or not isinstance(brief, dict) or not brief.get("id"):
            problems.append(f"briefs row for {target_str} missing")
        else:
            log.info("briefs: id=%s collected_at=%s",
                     brief.get("id"), brief.get("collected_at"))
    except Exception as e:
        log.error("briefs query failed: %s", e)
        problems.append("briefs query failed (infrastructure)")

    # 2) Garmin metrics — required (HRV excluded — see header docstring)
    try:
        r = sb.table("garmin_metrics").select(
            "body_battery,hrv,rhr,sleep_duration_min,total_steps,sleep_score"
        ).eq("date", target_str).maybe_single().execute()
        m = get_data(r)
        if not m or not isinstance(m, dict):
            problems.append(f"garmin_metrics row for {target_str} missing")
        else:
            # hrv omitted: Garmin API only returns HRV for settled days.
            required = ["body_battery", "rhr", "sleep_duration_min", "total_steps"]
            missing_fields = [f for f in required if m.get(f) is None]
            hrv_note = ""
            if m.get("hrv") is None:
                hrv_note = " (hrv=None — live mode, expected)"
            if missing_fields:
                problems.append(
                    f"garmin_metrics for {target_str} missing fields: {missing_fields}"
                )
            else:
                log.info("garmin_metrics: bb=%s hrv=%s rhr=%s sleep=%smin steps=%s score=%s%s",
                         m.get("body_battery"), m.get("hrv"), m.get("rhr"),
                         m.get("sleep_duration_min"), m.get("total_steps"),
                         m.get("sleep_score"), hrv_note)
    except Exception as e:
        log.error("garmin_metrics query failed: %s", e)
        problems.append("garmin_metrics query failed (infrastructure)")

    # 3) Weather — at least 1 row required
    try:
        r = sb.table("weather_log").select("id").eq("date", target_str).execute()
        rows = get_data(r) or []
        n = len(rows) if isinstance(rows, list) else 0
        if n < 1:
            problems.append(f"weather_log for {target_str}: 0 rows")
        else:
            log.info("weather_log: %d rows", n)
    except Exception as e:
        log.error("weather_log query failed: %s", e)
        problems.append("weather_log query failed (infrastructure)")

    # 4) Tasks — at least 1 row required
    try:
        r = sb.table("tasks").select("id").eq("date", target_str).execute()
        rows = get_data(r) or []
        n = len(rows) if isinstance(rows, list) else 0
        if n < 1:
            problems.append(f"tasks for {target_str}: 0 rows")
        else:
            log.info("tasks: %d rows", n)
    except Exception as e:
        log.error("tasks query failed: %s", e)
        problems.append("tasks query failed (infrastructure)")

    # 5) Calendar — 0+ OK, just log
    try:
        r = sb.table("calendar_events").select("id").eq("date", target_str).execute()
        rows = get_data(r) or []
        n = len(rows) if isinstance(rows, list) else 0
        log.info("calendar_events: %d rows (0+ OK)", n)
    except Exception as e:
        log.warning("calendar_events query failed (non-fatal): %s", e)

    # 6) Food — 0+ OK
    try:
        from datetime import timedelta
        food_date = (target - timedelta(days=1)).isoformat()
        r = sb.table("food_log").select("id").eq("date", food_date).execute()
        rows = get_data(r) or []
        n = len(rows) if isinstance(rows, list) else 0
        log.info("food_log for %s (yesterday): %d rows (0+ OK)", food_date, n)
    except Exception as e:
        log.warning("food_log query failed (non-fatal): %s", e)

    if problems:
        # Post-flight: WARN, don't block. Caller treats rc=1 as advisory.
        log.warning("preflight issues for %s (non-blocking):", target_str)
        for p in problems:
            log.warning("  - %s", p)
        log.warning("rc=1 (advisory only; render is still valid with partial data)")
        return 1

    log.info("preflight OK for %s — all required data present", target_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())