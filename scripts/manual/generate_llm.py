#!/usr/bin/env python3
"""generate_llm.py — generate narrative + 5 per-block opinions for one brief date.

Reads DB state, calls Hermes LLM via playful.narrative.compose / compose_all_opinions,
writes narrative JSON + opinion strings back into briefs.narrative and briefs.telegram_text.

Per Pitfall §6 from morning-brief-v2 skill: narrative+opinions ~120s wall-clock + LLM tokens.
On timeout, returns None and leaves DB untouched.

Usage:
    ./generate_llm.py --date 2026-07-07
    ./generate_llm.py --date 2026-07-07 --skip-opinions
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/root/morning_brief_v2")

# Load .env BEFORE supabase reads it
for _line in Path("/root/morning_brief_v2/.env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _, _v = _line.partition("=")
    os.environ.setdefault(_k, _v)

from playful.render_playful import fetch_live_context  # noqa: E402
from playful.narrative import compose, compose_all_opinions  # noqa: E402
from db.client import get_client, get_brief  # noqa: E402

# Make stderr strictly line-buffered so that when pult/worker.py runs this
# script via subprocess.Popen(stderr=PIPE), WARNING/ERROR log lines reach
# the worker on exit instead of being trapped in Python's 4KB block buffer.
# (PYTHONUNBUFFERED=1 set by the worker flushes stdout but not Python's
# logging — these are independent streams.)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try:
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass
log = logging.getLogger("generate_llm")


def _format_facts_for_narrative(ctx: dict) -> dict:
    """Reduce ctx to the keys compose() expects. Mirror of the narrative_facts
    block in render_playful.fetch_live_context() — must stay in sync.

    Real keys from fetch_live_context() (verified 2026-07-08):
      garmin / garmin_yesterday — dicts with body_battery, sleep_*,
        hrv, rhr, stress, spo2, totalSteps, etc.
      weather — list[{period, temp, condition, wind}]
      calendar — list[{title, start_time, duration_minutes}]
      tasks — list
      food — list of mapped rows
    """
    garmin = ctx.get("garmin") or {}
    garmin_y = ctx.get("garmin_yesterday") or {}
    sleep_min = garmin.get("sleep_duration_min")
    sleep_label = _minutes_to_label(sleep_min) if sleep_min else None
    sleep_score = garmin.get("sleep_score")
    # Mirror render_playful._sleep_pill logic loosely (just a string token)
    sleep_pill = None
    if sleep_score is not None:
        if sleep_score >= 90:
            sleep_pill = "good"
        elif sleep_score >= 75:
            sleep_pill = "amber"
        else:
            sleep_pill = "rose"

    # steps_yesterday + balance (same numbers the movement block uses)
    steps_yest = (garmin_y or {}).get("totalSteps") or (ctx.get("helio_yesterday") or {}).get("steps")
    kcal_burned_yest = ((garmin_y or {}).get("resting_kcal") or 0) + ((garmin_y or {}).get("active_kcal") or 0)
    kcal_eaten_yest = sum(((e or {}).get("kcal") or 0) for e in (ctx.get("food") or []))
    balance_yest = (
        kcal_eaten_yest - kcal_burned_yest
        if (kcal_eaten_yest or kcal_burned_yest) else None
    )

    # weather summary "morning/day/evening" for compose()
    weather_summary = None
    day_w = next((w for w in (ctx.get("weather") or []) if w.get("period") == "day"), None)
    if day_w:
        weather_summary = f"{day_w.get('condition', '?')}, {day_w.get('temp')}°"

    # top_task from tasks (by priority asc, then title)
    tasks_sorted = sorted(
        ctx.get("tasks") or [],
        key=lambda t: (t.get("priority") or 4, t.get("title") or ""),
    )
    top_task_title = (tasks_sorted[0].get("title") or "") if tasks_sorted else None

    brief_date_val = ctx.get("brief_date")
    brief_date_str = ""
    try:
        brief_date_str = brief_date_val.isoformat() if brief_date_val else ""
    except AttributeError:
        brief_date_str = str(brief_date_val or "")

    return {
        "brief_date": brief_date_str,
        "body_battery": garmin.get("body_battery"),
        "body_battery_delta": ctx.get("body_battery_delta"),
        "sleep_label": sleep_label,
        "sleep_score": sleep_score,
        "sleep_pill": sleep_pill,
        "hrv": garmin.get("hrv"),
        "rhr": garmin.get("rhr"),
        "stress": garmin.get("stress"),
        "spo2": garmin.get("spo2"),
        "steps_yesterday": steps_yest,
        "balance": balance_yest,
        "weather_summary": weather_summary,
        "tasks_count": len(ctx.get("tasks") or []),
        "top_task": top_task_title,
    }


def _build_block_facts(ctx: dict) -> dict[str, dict]:
    """Mirror fetch_live_context's block_facts for compose_all_opinions()."""
    garmin = ctx.get("garmin") or {}
    garmin_y = ctx.get("garmin_yesterday") or {}
    weather = ctx.get("weather") or []
    weather_day_facts = facts_dict_for("weather", weather)
    tasks_facts = facts_dict_for("tasks", ctx.get("tasks") or [], _top_task_title(ctx))
    kcal_burned_yest = ((garmin_y or {}).get("resting_kcal") or 0) + ((garmin_y or {}).get("active_kcal") or 0)
    kcal_eaten_yest = sum(((e or {}).get("kcal") or 0) for e in (ctx.get("food") or []))
    balance_yest = (
        kcal_eaten_yest - kcal_burned_yest
        if (kcal_eaten_yest or kcal_burned_yest) else None
    )
    movement_facts = {
        "steps_yesterday": (garmin_y or {}).get("totalSteps"),
        "balance_yesterday": balance_yest,
        "kcal_eaten_yesterday": kcal_eaten_yest,
        "kcal_burned_yesterday": kcal_burned_yest,
    }
    calendar_facts = facts_dict_for("calendar", ctx.get("calendar") or [])
    sleep_min = garmin.get("sleep_duration_min")
    sleep_label = _minutes_to_label(sleep_min) if sleep_min else None
    battery_facts = {
        "body_battery": garmin.get("body_battery"),
        "sleep_label": sleep_label,
        "sleep_score": garmin.get("sleep_score"),
        "hrv": garmin.get("hrv"),
    }
    return {
        "weather": weather_day_facts,
        "tasks": tasks_facts,
        "movement": movement_facts,
        "calendar": calendar_facts,
        "battery": battery_facts,
    }


def _minutes_to_label(minutes: int | None) -> str | None:
    """Mirror render_playful._minutes_to_label without importing the private fn."""
    if not minutes:
        return None
    h = minutes // 60
    m = minutes % 60
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def _top_task_title(ctx: dict) -> str | None:
    """Return highest-priority task title (matches narrative_facts.top_task)."""
    tasks_sorted = sorted(
        ctx.get("tasks") or [],
        key=lambda t: (t.get("priority") or 4, t.get("title") or ""),
    )
    return (tasks_sorted[0].get("title") or "") if tasks_sorted else None


def facts_dict_for(block: str, rows, top_task: str | None = None):
    """Mirror render_playful.facts_dict_for (private). Inline copy because
    the canonical version lives inside fetch_live_context and isn't exported."""
    if block == "weather":
        out = {}
        for w in rows or []:
            period = w.get("period")
            if not period:
                continue
            out[period] = {
                "temp":         w.get("temp"),
                "condition":    w.get("condition"),
                "wind":         w.get("wind"),
                # LLM opinion prompt expects temp_<period> / condition_<period>;
                # without these, _compose_block_async gets empty facts and fabricates.
                "condition_day": w.get("condition") if period == "day" else None,
                "temp_day":      w.get("temp")      if period == "day" else None,
            }
        return out
    if block == "tasks":
        return {
            "count": len(rows or []),
            "items": (rows or [])[:5],
            "top_task": top_task,
        }
    if block == "calendar":
        return {
            "count": len(rows or []),
            "items": (rows or [])[:5],
        }
    return {}


def _derive_telegram_text(payload: dict) -> str:
    """Derive short Telegram-friendly plain-text from narrative JSON.

    No LLM call. Reads:
      headline, lead, footer_text
    from payload and assembles a markdown-ready string ≤ 1024 chars.
    """
    headline = (payload.get("headline") or "Утренний бриф").strip()
    lead = (payload.get("lead") or "").strip()
    footer = (payload.get("footer_text") or "").strip()
    parts = [f"*{headline}*", ""]
    if lead:
        parts.append(lead)
        parts.append("")
    if footer:
        parts.append(f"→ {footer}")
    text = "\n".join(parts).strip()
    # Telegram hard limit 4096, soft target ~1024
    if len(text) > 1024:
        text = text[:1021].rstrip() + "…"
    return text


# ── Deterministic opinions fallback (Pitfall §27) ─────────────────────────
# When compose_all_opinions returns None for a block (LLM timeout/empty),
# don't leave the opinion empty. Use a short, factual, non-LLM string.
def _fallback_opinion(block: str, facts_ctx: dict) -> str | None:
    if block == "weather":
        day_w = next(
            (w for w in (facts_ctx.get("weather") or []) if w.get("period") == "day"),
            None,
        )
        if not day_w:
            return None
        return f"Днём {day_w.get('temp')}°, {day_w.get('condition', '?').lower()} — окно для дел на улице."

    if block == "tasks":
        tasks = facts_ctx.get("tasks") or []
        if not tasks:
            return None
        p3 = [t for t in tasks if (t.get("priority") or 4) == 3]
        if p3:
            return f"{len(tasks)} задач, из них {len(p3)} на p3 — закрой одну из них до обеда."
        return f"{len(tasks)} задач на сегодня. Начни с самой приоритетной."

    if block == "movement":
        gy = facts_ctx.get("garmin_yesterday") or {}
        steps = gy.get("totalSteps")
        if not steps:
            return None
        if steps < 5000:
            return f"Вчера {steps} шагов — мало. Сегодня добери хотя бы до 7к."
        if steps < 9000:
            return f"Вчера {steps} шагов — нормально, но без запаса. Пройдись лишний раз."
        return f"Вчера {steps} шагов — хороший день. Не сбавляй темп."

    if block == "calendar":
        cal = facts_ctx.get("calendar") or []
        if not cal:
            return "Встреч нет — свободный день. Потрать его на главную задачу."
        deep = [c for c in cal if "deep" in (c.get("title") or "").lower()]
        if deep:
            return f"{len(cal)} встреч, есть Deep Work — идеальный день для главного."
        return f"{len(cal)} встреч(и) сегодня. Между ними — фокус на главном."

    if block == "battery":
        g = facts_ctx.get("garmin") or {}
        bb = g.get("body_battery")
        sleep_label = _minutes_to_label(g.get("sleep_duration_min"))
        sleep_score = g.get("sleep_score")
        hrv = g.get("hrv")
        bits = []
        if bb is not None:
            bits.append(f"BB={bb}")
        if sleep_label:
            bits.append(f"сон {sleep_label}" + (f" (score {sleep_score})" if sleep_score is not None else ""))
        if hrv is not None:
            bits.append(f"HRV {hrv}")
        if not bits:
            return None
        return "Ресурс: " + ", ".join(bits) + " — бери главное до обеда, потом просядет."

    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--skip-opinions", action="store_true",
                   help="Skip compose_all_opinions (narrative only, faster)")
    p.add_argument("--write", action="store_true",
                   help="Actually write to DB (default: dry-run, print only)")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    log.info("[%s] fetching context from DB", target)
    ctx = fetch_live_context(target)

    facts = _format_facts_for_narrative(ctx)
    log.info("[%s] calling narrative.compose (hermes -z, 120s timeout)", target)
    narrative = compose(facts, timeout=120)
    if not narrative:
        log.warning("[%s] narrative returned None — DB will not be written", target)
        return 2

    opinions: dict[str, str | None] = {}
    if not args.skip_opinions:
        block_facts = _build_block_facts(ctx)
        log.info("[%s] calling compose_all_opinions (5 sequential)", target)
        try:
            opinions = asyncio.run(compose_all_opinions(block_facts))
        except Exception as e:
            log.warning("[%s] compose_all_opinions failed: %s", target, e)
            opinions = {}

    payload = {
        "headline":     narrative.get("headline"),
        "lead":         narrative.get("lead"),
        "footer_title": narrative.get("footer_title"),
        "footer_text":  narrative.get("footer_text"),
        "opinions":     {k: v for k, v in opinions.items() if v},
    }
    # Per-block fallback for missing opinions (Pitfall §27).
    # If LLM returned None for a block, use a deterministic string from DB facts.
    from playful.narrative import OPINION_BLOCKS as _OPINION_BLOCKS
    for block_name in _OPINION_BLOCKS:
        if not payload["opinions"].get(block_name):
            fb = _fallback_opinion(block_name, ctx)
            if fb:
                payload["opinions"][block_name] = fb

    narrative_json = json.dumps(payload, ensure_ascii=False)
    telegram_text = _derive_telegram_text(payload)

    log.info("[%s] narrative JSON ready (%d bytes)", target, len(narrative_json))
    log.info("  headline: %s", payload["headline"])
    log.info("  telegram_text preview: %s", telegram_text[:120].replace("\n", " ⏎ "))
    for blk, op in payload["opinions"].items():
        log.info("  opinion[%s]: %s", blk, (op or "")[:80])

    if not args.write:
        log.info("[%s] DRY-RUN: pass --write to persist to briefs.narrative + briefs.telegram_text", target)
        # Print the full payload + telegram_text so the user can see what would be written
        print("=" * 60)
        print("PAYLOAD (narrative JSON):")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print()
        print("TELEGRAM TEXT (derived, no LLM):")
        print(telegram_text)
        return 0

    brief = get_brief(target)
    if not brief:
        log.error("[%s] no brief row, abort", target)
        return 3
    brief_id = brief["id"]

    sb = get_client()
    sb.table("briefs").update({
        "narrative":     narrative_json,
        "telegram_text": telegram_text,
    }).eq("id", brief_id).execute()
    log.info("[%s] wrote narrative (%d bytes) + telegram_text (%d bytes) to briefs id=%s",
             target, len(narrative_json), len(telegram_text), brief_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())