#!/usr/bin/env python3
"""generate_llm_blocks.py — per-block LLM narrative (2-4 предложения на блок).

Развитие generate_llm.py: вместо одного монолитного вызова — отдельные
блоки (weather/tasks/movement/calendar/battery), каждый со своим нарративом.
Результат пишется в briefs.narrative_blocks (jsonb) и briefs.narrative_blocks_meta.

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
from datetime import date, datetime
from pathlib import Path

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
                   help="Persist to briefs.narrative_blocks (default: dry-run)")
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
        text, meta = asyncio.run(_one_block(blk, facts_by_block[blk], args.timeout))
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
        log.info("[%s] DRY-RUN: pass --write to persist to brief_block_narratives", target)
        print("=" * 60)
        print("NARRATIVE BLOCKS (would be written to brief_block_narratives):")
        print(json.dumps(out_text, ensure_ascii=False, indent=2))
        print()
        print("META:")
        print(json.dumps(out_meta, ensure_ascii=False, indent=2))
        return 0

    # Persist to brief_block_narratives (UPSERT per (brief_id, block_name)).
    # History-friendly: каждый перезапуск блока пишет новую строку (а не
    # patch поверх), upsert ON CONFLICT заменяет существующую.
    brief = get_brief(target)
    if not brief:
        log.error("[%s] no brief row, abort", target)
        return 3
    brief_id = brief["id"]

    for blk in targets:
        text = out_text.get(blk)
        if not text:
            log.warning("[%s] block %s has no text, skipping upsert", target, blk)
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

    return 0


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


if __name__ == "__main__":
    sys.exit(main())