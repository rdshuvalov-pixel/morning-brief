#!/usr/bin/env python3
"""generate_llm_blocks.py — per-block LLM narrative (2-4 предложения на блок).

Развитие generate_llm.py: вместо одного монолитного вызова — отдельные
блоки (weather/tasks/movement/calendar/battery), каждый со своим нарративом.
Результат пишется в пять briefs.narrative_{block} колонок и briefs.narrative_blocks_meta.

Использование:
    # один блок, dry-run (без записи в БД)
    ./generate_llm_blocks.py --date 2026-07-17 --block weather

    # все 5 блоков (sequential), dry-run
    ./generate_llm_blocks.py --date 2026-07-17 --all

    # один блок + записать в БД
    ./generate_llm_blocks.py --date 2026-07-17 --block weather --write

    # все 5 + записать
    ./generate_llm_blocks.py --date 2026-07-17 --all --write

Pitfall §18: по умолчанию dry-run. --write нужен только когда уверен.
Pitfall §44: при --write читаем существующий narrative_blocks, чтобы
перегенерировать только указанный блок, не затирая остальные.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable, Sequence

sys.path.insert(0, "/root/morning_brief_v2")

# Load .env BEFORE supabase reads it (mirror generate_llm.py)
for _line in Path("/root/morning_brief_v2/.env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _, _v = _line.partition("=")
    os.environ.setdefault(_k, _v)

from playful.render_playful import fetch_live_context  # noqa: E402
from playful.narrative_blocks import (  # noqa: E402
    NARRATIVE_BLOCKS,
    compose_block,
    compose_all_blocks,
)
from db.client import get_client, get_brief, upsert_narrative_block  # noqa: E402

# Strictly line-buffered stderr so worker.py captures WARNING/ERROR on exit.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try:
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass
log = logging.getLogger("generate_llm_blocks")

LLM_ATTEMPTS = 2  # per block (preview is bounded by TIMEOUTS['llm'] in worker.py)
LLM_RETRY_DELAY_SEC = 5
LLM_HEALTHCHECK_TIMEOUT = 15  # hard preflight — if hermes -z 'ping' fails, skip ALL LLM calls immediately and write fallback blocks

import shutil
import subprocess as _sp


def _hermes_healthcheck(log) -> bool:
    """Cheap preflight: `hermes -z "ping"` with a tight timeout. If the LLM
    backend is currently serving 404s (observed 2026-07-22 11:15-11:19),
    skip every block call and write deterministic fallbacks instead —
    saves the ~75-200s budget for nothing and prevents worker SIGTERM. Returns
    True if hermes responded with a sensible Russian/English answer; False on
    timeout, error-pattern response, or non-zero exit.
    """
    hermes_bin = shutil.which("hermes") or "/usr/local/lib/hermes-agent/venv/bin/hermes"
    try:
        proc = _sp.run(
            [hermes_bin, "-z", "ping"],
            capture_output=True, text=True, timeout=LLM_HEALTHCHECK_TIMEOUT,
        )
    except _sp.TimeoutExpired:
        log.warning("llm-healthcheck: hermes -z 'ping' timed out after %ds → all blocks will use fallback",
                    LLM_HEALTHCHECK_TIMEOUT)
        return False
    except Exception as e:
        log.warning("llm-healthcheck: spawn failed: %s", e)
        return False

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        log.warning("llm-healthcheck: rc=%d stderr=%s",
                    proc.returncode, (proc.stderr or "")[:200])
        return False
    low = out.lower()
    # Common error fragments observed when the upstream backend is sick
    for needle in ("api call failed", "http 4", "http 5", "404", "401",
                   "rate limit", "i cannot", "i'm sorry"):
        if needle in low:
            log.warning("llm-healthcheck: response contains %r (rc=%d) → fallback mode",
                        needle, proc.returncode)
            return False
    log.info("llm-healthcheck: hermes responded ok (%d chars)", len(out))
    return True


def _build_block_facts_for_narrative(ctx: dict) -> dict[str, dict]:
    """Mirror generate_llm._build_block_facts but with extras (vs_yesterday, items, steps_goal).

    Differences from opinion facts:
      - weather: include vs_yesterday comparison string
      - tasks: include items list (top-5) for richer prompts
      - movement: include steps_goal hint
      - calendar: include items list with start_time
      - battery: identical to opinion facts
    """
    garmin = ctx.get("garmin") or {}
    garmin_y = ctx.get("garmin_yesterday") or {}
    weather = ctx.get("weather") or []
    weather_yest = ctx.get("weather_yesterday") or []

    # weather — derive vs_yesterday
    vs_yesterday_parts = []
    day_w = next((w for w in weather if w.get("period") == "day"), None)
    day_y = next((w for w in weather_yest if w.get("period") == "day"), None) if weather_yest else None
    if day_w and day_y and day_w.get("temp") is not None and day_y.get("temp") is not None:
        try:
            d = round(float(day_w["temp"]) - float(day_y["temp"]))
            if d >= 2:
                vs_yesterday_parts.append(f"днём на {d}° теплее")
            elif d <= -2:
                vs_yesterday_parts.append(f"днём на {abs(d)}° прохладнее")
        except (ValueError, TypeError):
            pass
    vs_yesterday = "; ".join(vs_yesterday_parts) if vs_yesterday_parts else None

    weather_facts: dict[str, object] = {"vs_yesterday": vs_yesterday}
    for w in weather:
        period = w.get("period")
        if not period:
            continue
        weather_facts[period] = {
            "temp": w.get("temp"),
            "condition": w.get("condition"),
            "wind": w.get("wind"),
            f"temp_{period}": w.get("temp"),
            f"condition_{period}": w.get("condition"),
        }

    # tasks
    tasks = ctx.get("tasks") or []
    p3_count = sum(1 for t in tasks if (t.get("priority") or 4) == 3)
    tasks_sorted = sorted(tasks, key=lambda t: (t.get("priority") or 4, t.get("title") or ""))
    top_task = (tasks_sorted[0].get("title") if tasks_sorted else None)
    tasks_facts = {
        "count": len(tasks),
        "p3_count": p3_count,
        "top_task": top_task,
        "items": [{"title": t.get("title"), "priority": t.get("priority")} for t in tasks_sorted[:5]],
    }

    # movement
    steps_yest = (garmin_y or {}).get("totalSteps") or (ctx.get("helio_yesterday") or {}).get("steps")
    kcal_burned = ((garmin_y or {}).get("resting_kcal") or 0) + ((garmin_y or {}).get("active_kcal") or 0)
    kcal_eaten = sum(((e or {}).get("kcal") or 0) for e in (ctx.get("food") or []))
    balance = (kcal_eaten - kcal_burned) if (kcal_eaten or kcal_burned) else None
    movement_facts = {
        "steps_yesterday": steps_yest,
        "steps_goal": 7000,  # hardcoded — render_playful uses this constant
        "balance_yesterday": balance,
        "kcal_eaten_yesterday": kcal_eaten,
        "kcal_burned_yesterday": kcal_burned,
    }

    # calendar
    cal = ctx.get("calendar") or []
    cal_facts = {
        "meetings_count": len(cal),
        "deepwork_minutes": 0,  # not derived here — render_playful._build_deep_work_slots does it
        "free_day": (len(cal) == 0),
        "items": [{"title": c.get("title"), "start_time": str(c.get("start_time") or "")}
                  for c in cal[:5]],
    }

    # battery (reuse opinion facts format)
    sleep_min = garmin.get("sleep_duration_min")
    battery_facts = {
        "body_battery": garmin.get("body_battery"),
        "body_battery_delta": ctx.get("body_battery_delta"),
        "sleep_label": _minutes_to_label(sleep_min),
        "sleep_score": garmin.get("sleep_score"),
        "hrv": garmin.get("hrv"),
        "rhr": garmin.get("rhr"),
        "stress": garmin.get("stress"),
    }

    return {
        "weather": weather_facts,
        "tasks": tasks_facts,
        "movement": movement_facts,
        "calendar": cal_facts,
        "battery": battery_facts,
    }


def _minutes_to_label(minutes: int | None) -> str | None:
    if not minutes:
        return None
    h = minutes // 60
    m = minutes % 60
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    block_group = p.add_mutually_exclusive_group(required=True)
    block_group.add_argument("--block", choices=NARRATIVE_BLOCKS,
                             help="Single block to (re)generate")
    block_group.add_argument("--all", action="store_true",
                             help="All 5 blocks sequentially")
    p.add_argument("--write", action="store_true",
                   help="Persist to briefs.narrative_{block} columns (default: dry-run)")
    p.add_argument("--with-narrative", action="store_true",
                   help="(only with --all) Also call narrative.compose() and write "
                        "briefs.narrative (headline/lead/footer) + telegram_text. "
                        "The /pult LLM button uses this to fill the complete brief row.")
    p.add_argument("--timeout", type=int, default=90,
                   help="Per-block hermes timeout in seconds (default 90)")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    log.info("[%s] fetching context from DB", target)
    ctx = fetch_live_context(target)
    facts_by_block = _build_block_facts_for_narrative(ctx)
    # Inject brief_date into every block facts (for prompt header)
    for blk in facts_by_block.values():
        blk["brief_date"] = target.isoformat()

    targets = list(NARRATIVE_BLOCKS) if args.all else [args.block]
    log.info("[%s] composing block narratives: %s (timeout=%ds, write=%s)",
             target, targets, args.timeout, args.write)

    # Run sequentially (Hermes gateway serializes backend anyway — see
    # playful/narrative.py:354). For --all this means ~75-200s total.
    out_text: dict[str, str | None] = {}
    out_meta: dict[str, dict] = {}
    for blk in targets:
        text, meta = asyncio.run(_one_block_with_retries(
            blk,
            facts_by_block[blk],
            args.timeout,
        ))
        out_text[blk] = text
        out_meta[blk] = meta

    # Print summary
    log.info("[%s] block narratives ready:", target)
    for blk in targets:
        text = out_text[blk] or ""
        src = out_meta[blk].get("source", "?")
        preview = text[:120].replace("\n", " ⏎ ") if text else "(empty)"
        log.info("  %s [%s, %d chars]: %s", blk, src, len(text), preview)

    if not args.write:
        log.info("[%s] DRY-RUN: pass --write to persist to briefs.narrative_*", target)
        print("=" * 60)
        print("NARRATIVE BLOCKS (would be written to briefs.narrative_*):")
        print(json.dumps(out_text, ensure_ascii=False, indent=2))
        print()
        print("META:")
        print(json.dumps(out_meta, ensure_ascii=False, indent=2))
        return 0

    # Persist to the five briefs.narrative_{block} columns.
    brief = get_brief(target)
    if not brief:
        # 2026-07-22: previously aborted with rc=3 "no brief row". Now we
        # create the row via upsert_brief() so the LLM-button works even
        # if Render+publish was never pressed. The fallback-assembly path
        # in _maybe_write_narrative() (--with-narrative) depends on having
        # *some* briefs.id to PATCH.
        log.warning("[%s] no brief row — creating via upsert_brief()", target)
        try:
            from db.client import upsert_brief
            brief = upsert_brief(target.isoformat())
        except Exception as e:
            log.error("[%s] upsert_brief failed: %s", target, e)
        if not brief or not brief.get("id"):
            log.error("[%s] cannot create brief row, abort", target)
            return 3
    brief_id = brief["id"]

    write_failed: list[str] = []
    for blk in targets:
        text = out_text.get(blk)
        if not text:
            log.warning("[%s] block %s has no text, skipping upsert", target, blk)
            write_failed.append(f"narrative_{blk}:empty")
            continue
        meta = out_meta.get(blk, {})
        try:
            upsert_narrative_block(
                brief_id=brief_id,
                block_name=blk,
                text=text,
                source=meta.get("source", "llm"),
                model=meta.get("model"),
                chars=meta.get("chars"),
                error=meta.get("error"),
            )
            log.info("[%s] wrote block %s (%d chars, source=%s)",
                     target, blk, len(text), meta.get("source"))
        except Exception as e:
            log.error("[%s] failed to upsert block %s: %s", target, blk, e)
            write_failed.append(f"narrative_{blk}:write-failed")

    # Optional: also call narrative.compose() to populate briefs.narrative
    # (headline/lead/footer) and briefs.telegram_text. Without this step,
    # the main page renders a "narrative-NLG not connected" placeholder even
    # though per-block narratives are present. Triggered by --with-narrative.
    # Only meaningful with --all (single-block mode is for spot-fix use).
    narrative_written = False
    if args.with_narrative:
        if not args.all:
            log.warning("[%s] --with-narrative ignored (requires --all)", target)
        else:
            narrative_written = _maybe_write_narrative(target, ctx, log)

    incomplete = write_failed + _collect_incomplete_outputs(
        targets,
        out_text,
        out_meta,
        require_narrative=bool(args.with_narrative and args.all),
        narrative_written=narrative_written,
    )
    if incomplete:
        log.error("[%s] incomplete LLM write: %s", target, ", ".join(dict.fromkeys(incomplete)))
        return 2

    return 0


def _maybe_write_narrative(target, ctx, log) -> bool:
    """Call narrative.compose() + write briefs.narrative + telegram_text.

    Delegates to generate_llm.py's _format_facts_for_narrative (the canonical
    fact shape for the headline/lead/footer prompt) and compose() (the
    canonical hermes -z call).

    Robustness contract (added 2026-07-22 after the morning-brief outage):
    ALWAYS write briefs.narrative + briefs.telegram_text — even if compose()
    returns None (e.g. LLM backend 404, hermes -z timeout). When compose()
    fails, assemble headline/lead/footer from the per-block narratives already
    written to briefs.narrative_* — deterministic, fact-grounded, not as
    lively as a real LLM pass, but it guarantees the morning Telegram send
    and the site render both have content.

    Returns True iff write succeeded (whichever path produced the payload).
    Returns False only when there is genuinely nothing to write.
    """
    try:
        # Import here to avoid module-level cycles (generate_llm imports this
        # file's playful.render_playful transitively, but the other way is fine).
        from scripts.manual.generate_llm import (
            _format_facts_for_narrative,
            _derive_telegram_text,
            _fallback_opinion,
        )
        from playful.narrative import compose, OPINION_BLOCKS
    except Exception as e:
        log.error("[%s] cannot import narrative helpers: %s", target, e)
        return False

    try:
        facts = _format_facts_for_narrative(ctx)
    except Exception as e:
        log.error("[%s] _format_facts_for_narrative failed: %s", target, e)
        return False

    log.info("[%s] calling narrative.compose (headline/lead/footer, 120s timeout)", target)
    narrative = _compose_narrative_with_retries(facts, log, compose_fn=compose)
    payload_source = "llm"  # tag for later diagnosis

    if not narrative:
        # ── FALLBACK PATH (2026-07-22): never leave briefs.narrative NULL when
        # per-block narratives are present. Assemble headline/lead/footer from
        # the just-written briefs.narrative_* columns so the site render and
        # Telegram send both work the next morning.
        log.warning("[%s] narrative.compose returned None — assembling from per-block fallbacks", target)
        try:
            from db.client import get_client, get_brief
            sb = get_client()
            brief = get_brief(target)
            if not brief:
                log.error("[%s] no brief row, cannot write narrative", target)
                return False
            r = sb.schema("morning_brief_v2").from_("briefs").select(
                "narrative_weather,narrative_tasks,narrative_movement,"
                "narrative_calendar,narrative_battery"
            ).eq("id", brief["id"]).execute()
            row = (r.data or [{}])[0]
            blocks = {k: (row.get(k) or "").strip() for k in
                      ("narrative_weather","narrative_tasks",
                       "narrative_movement","narrative_calendar",
                       "narrative_battery")}
            # Headline — prefer battery block, else first non-empty block.
            # Use .get() everywhere; missing keys fall back to a stub
            # rather than raising KeyError (2026-07-22: per-blocks may be
            # empty strings, and the headline/lead/footer logic must still
            # produce *something* useful for the user).
            headline_src = blocks.get("narrative_battery") or \
                next((v for v in blocks.values() if v), "Утренний бриф")
            headline = headline_src.rstrip(".").split(".")[0]
            if not headline.endswith("."):
                headline = headline + "." if len(headline) < 80 else headline
            # Lead — flow: weather → tasks → movement
            lead_keys = ("narrative_weather", "narrative_tasks", "narrative_movement")
            lead_chunks = [blocks[k] for k in lead_keys if blocks.get(k)]
            lead = " ".join(lead_chunks).strip()
            if not lead:
                # Per-blocks were all empty — fallback to the headline as a last resort
                lead = headline_src if headline_src != "Утренний бриф" else "День ждёт плана."
            # Footer — calendar hint
            cal = blocks.get("narrative_calendar", "")
            footer_title = "Куда сфокусироваться"
            if cal:
                footer_text = cal.rstrip(".") + ("." if not cal.endswith(".") else "")
            else:
                footer_text = "Действуй по плану: начни с главного."
            narrative = {
                "headline":     headline,
                "lead":         lead,
                "footer_title": footer_title,
                "footer_text":  footer_text,
            }
            payload_source = "fallback"
        except Exception as e:
            log.error("[%s] fallback narrative assembly failed: %s", target, e)
            return False

    # Per-block opinions: keep for compatibility with render_playful (still reads
    # them from briefs.narrative->opinions). Use deterministic fallback where
    # LLM compose_all_opinions is not run here — fill in what we already wrote
    # to per-block narratives (best-effort, not via LLM).
    from db.client import get_client, get_brief
    sb = get_client()
    opinions: dict[str, str] = {}
    for blk in OPINION_BLOCKS:
        fb = _fallback_opinion(blk, ctx)
        if fb:
            opinions[blk] = fb

    payload = {
        "headline":     narrative.get("headline"),
        "lead":         narrative.get("lead"),
        "footer_title": narrative.get("footer_title"),
        "footer_text":  narrative.get("footer_text"),
        "opinions":     opinions,
        "_source":      payload_source,  # diagnostic, not rendered
    }
    narrative_json = json.dumps(payload, ensure_ascii=False)
    telegram_text = _derive_telegram_text(payload)

    log.info("[%s] narrative JSON ready (%d bytes, source=%s)", target, len(narrative_json), payload_source)
    log.info("  headline: %s", payload["headline"])
    log.info("  telegram_text preview: %s",
             telegram_text[:120].replace("\n", " ⏎ "))

    brief = get_brief(target)
    if not brief:
        log.error("[%s] no brief row, cannot write narrative", target)
        return False
    brief_id = brief["id"]

    try:
        sb.schema("morning_brief_v2").from_("briefs").update({
            "narrative":     narrative_json,
            "telegram_text": telegram_text,
        }).eq("id", brief_id).execute()
        log.info("[%s] wrote narrative (%d bytes, source=%s) + telegram_text (%d bytes)",
                 target, len(narrative_json), payload_source, len(telegram_text))
        return True
    except Exception as e:
        log.error("[%s] failed to update briefs.narrative: %s", target, e)
        return False


def _compose_narrative_with_retries(
    facts: dict,
    log,
    *,
    attempts: int = LLM_ATTEMPTS,
    retry_delay: float = LLM_RETRY_DELAY_SEC,
    timeout: int = 120,
    compose_fn: Callable[..., dict[str, str] | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, str] | None:
    """Retry the global headline/lead/footer LLM call on transient failure."""
    if compose_fn is None:
        from playful.narrative import compose as compose_fn

    for attempt in range(1, max(1, attempts) + 1):
        result = compose_fn(facts, timeout=timeout)
        if result:
            return result
        if attempt < attempts:
            log.warning("narrative.compose attempt %d/%d failed; retrying in %.1fs",
                        attempt, attempts, retry_delay)
            sleep_fn(retry_delay)
    return None


def _collect_incomplete_outputs(
    targets: Sequence[str],
    out_text: dict[str, str | None],
    out_meta: dict[str, dict],
    *,
    require_narrative: bool,
    narrative_written: bool,
) -> list[str]:
    """Return missing/degraded DB fields for the LLM button completion gate."""
    incomplete: list[str] = []
    for blk in targets:
        if not out_text.get(blk):
            incomplete.append(f"narrative_{blk}:empty")
        elif out_meta.get(blk, {}).get("source") != "llm":
            incomplete.append(f"narrative_{blk}:not-llm")
    if require_narrative and not narrative_written:
        incomplete.extend(("narrative", "telegram_text"))
    return incomplete


async def _one_block(name: str, facts: dict, timeout: int) -> tuple[str | None, dict]:
    """Wrapper to capture source + meta from compose_block via meta in facts.

    compose_block returns just text — we re-derive meta by calling the underlying
    async helper directly.
    """
    from playful.narrative_blocks import _compose_block_narrative_async
    from datetime import datetime, timezone
    text, source, err = await _compose_block_narrative_async(name, facts, timeout=timeout)
    meta = {
        "source": source,
        "error": err or None,
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": "hermes-gateway",
        "chars": len(text) if text else 0,
    }
    return (text or None), meta


async def _one_block_with_retries(
    name: str,
    facts: dict,
    timeout: int,
    *,
    attempts: int = LLM_ATTEMPTS,
    retry_delay: float = LLM_RETRY_DELAY_SEC,
    one_block_fn: Callable[[str, dict, int], Awaitable[tuple[str | None, dict]]] = _one_block,
) -> tuple[str | None, dict]:
    """Retry a block when Hermes produced fallback/empty instead of real LLM text."""
    last: tuple[str | None, dict] = (None, {"source": "empty", "error": "not-run"})
    for attempt in range(1, max(1, attempts) + 1):
        last = await one_block_fn(name, facts, timeout)
        text, meta = last
        if text and meta.get("source") == "llm":
            return last
        if attempt < attempts:
            log.warning("block %s attempt %d/%d returned source=%s error=%s; retrying in %.1fs",
                        name, attempt, attempts, meta.get("source"), meta.get("error"), retry_delay)
            if retry_delay:
                await asyncio.sleep(retry_delay)
    return last


if __name__ == "__main__":
    sys.exit(main())