#!/usr/bin/env python3
"""generate_weekly_recap.py — collect + LLM-summarise the last completed week.

Pipeline:
  1. Determine last completed Mon..Sun range (Europe/Lisbon).
  2. Query Supabase tables (garmin_metrics, food_log, calendar_events, tasks)
     for that range, aggregate weekly stats.
  3. Call playful.narrative_weekly.compose(facts) -> 5-field dict.
  4. Format and send via Telegram Bot API (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).
  5. If --dry-run: print facts + composed text, do NOT send to TG.
  6. If --no-llm: build deterministic text from facts only (no LLM call).
  7. If --send-only <file>: read cached JSON, format & send (manual recovery).

Sends via /usr/local/lib/hermes-agent/.../bin-style Bot API directly — does
NOT depend on briefs.telegram_text (banned) or Hermes CLI.

Usage:
    ./generate_weekly_recap.py                    # last completed week, LLM, send to TG
    ./generate_weekly_recap.py --dry-run         # compute + show, no LLM, no send
    ./generate_weekly_recap.py --no-llm          # deterministic recap, send to TG
    ./generate_weekly_recap.py --week-of 2026-07-05  # week containing that date (Mon-based)
    ./generate_weekly_recap.py --send-only /tmp/weekly.json  # send cached narrative
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
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

from db.client import get_client  # noqa: E402
from playful.narrative_weekly import (  # noqa: E402
    compose as compose_narrative,
    render_for_telegram,
    render_for_telegram_pages,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weekly_recap")


# ─────────────────────────────────────────────────────────────────────
# Week range: last completed Mon..Sun in Europe/Lisbon
# ─────────────────────────────────────────────────────────────────────

def _last_completed_week(today: date | None = None) -> tuple[date, date]:
    """Return (monday, sunday) of the most recently completed calendar week.

    Mon-based ISO week. If today is Mon (Lisbon), previous week is the one
    that just ended YESTERDAY.

    Per Skill §33 (Europe/Lisbon bias) — always use Lisbon civil date for 'today'.
    """
    if today is None:
        # Lisbon civil date (Lisbon == Europe/Lisbon)
        today = datetime.now(tz=_lisbon_tz()).date()
    # weekday(): Mon=0..Sun=6. Subtract (weekday + 7) to land on previous Monday.
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _lisbon_tz():
    from datetime import timezone
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Lisbon")
    except Exception:
        return timezone.utc  # fallback — Lisbon is UTC+0/1, monotonic drift tolerable


def _week_of(date_val: date) -> tuple[date, date]:
    """Return (monday, sunday) of the calendar week containing date_val."""
    monday = date_val - timedelta(days=date_val.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ─────────────────────────────────────────────────────────────────────
# Supabase data collection
# ─────────────────────────────────────────────────────────────────────

GARMIN_COLS = (
    "date, sleep_duration_min, sleep_score, deep_sleep_pct, hrv, body_battery, "
    "rhr, stress, total_steps, distance_km"
)
FOOD_COLS = "date, meal_name, kcal, protein, fat, carbs"
CAL_COLS = "date, title, start_time, duration_minutes"
TASK_COLS = "date, title, priority"


def _fetch_range(table: str, select: str, start: date, end: date) -> list[dict]:
    """Fetch rows where date BETWEEN start..end inclusive.

    Robust against supabase-py version differences (data attribute vs direct list).
    """
    sb = get_client()
    res = (
        sb.table(table)
        .select(select)
        .gte("date", str(start))
        .lte("date", str(end))
        .execute()
    )
    data = res.data
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]  # type: ignore[return-value]  # pyright happy


def _aggregate_garmin(rows: list[dict]) -> dict:
    if not rows:
        return {}
    days = len({r["date"] for r in rows})

    def avg(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(statistics.mean(vals), 1) if vals else None

    def mn(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return min(vals) if vals else None

    def tot(field):
        return sum(r[field] for r in rows if r.get(field) is not None) or None

    steps_vals = [r["total_steps"] for r in rows if r.get("total_steps") is not None]
    dist_vals  = [r["distance_km"] for r in rows if r.get("distance_km")  is not None]
    return {
        "days": days,
        "mean_sleep_min": avg("sleep_duration_min"),
        "mean_sleep_score": avg("sleep_score"),
        "mean_hrv": avg("hrv"),
        "mean_rhr": avg("rhr"),
        "mean_body_battery": avg("body_battery"),
        "mean_stress": avg("stress"),
        "min_deep_pct": mn("deep_sleep_pct"),
        # Movement (Migration 006_garmin_total_steps: total_steps + distance_km)
        "sum_steps": tot("total_steps"),
        "mean_steps": round(statistics.mean(steps_vals), 0) if steps_vals else None,
        "sum_distance_km": round(sum(dist_vals), 1) if dist_vals else None,
        "mean_distance_km": round(statistics.mean(dist_vals), 2) if dist_vals else None,
        "steps_days_with_data": len(steps_vals),
        "distance_days_with_data": len(dist_vals),
    }


def _aggregate_food(rows: list[dict]) -> dict:
    if not rows:
        return {}
    by_day: dict[str, int] = defaultdict(int)
    by_day_protein: dict[str, float] = defaultdict(float)
    for r in rows:
        d = r["date"]
        if r.get("kcal") is not None:
            by_day[d] += r["kcal"]
        if r.get("protein") is not None:
            by_day_protein[d] += r["protein"]
    days = list(by_day.keys())
    log_days = len({r["date"] for r in rows})
    total_kcal = sum(by_day.values())
    total_protein = sum(by_day_protein.values())

    cheat_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, None)

    meals_by_kcal = sorted(
        ((r["meal_name"], r.get("kcal") or 0) for r in rows),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_meal = meals_by_kcal[0][0] if meals_by_kcal and meals_by_kcal[0][1] > 0 else None

    return {
        "log_days": log_days,
        "sum_kcal": total_kcal if total_kcal else None,
        # mean per DAY (not per row) — sum of day-totals / unique days
        "mean_kcal_per_day": round(total_kcal / len(days), 0) if days else None,
        "mean_protein_g": round(total_protein / len(days), 1) if days else None,
        "top_meal_by_kcal": top_meal,
        "cheat_day_kcal": cheat_day[1] if cheat_day[0] else None,
    }


def _aggregate_calendar(rows: list[dict]) -> dict:
    if not rows:
        return {}
    days_meetings = Counter(r["date"] for r in rows)
    busiest_day, _ = max(days_meetings.items(), key=lambda kv: kv[1]) if days_meetings else (None, None)
    durs = [r["duration_minutes"] for r in rows if r.get("duration_minutes") is not None]
    longest = max(
        ((r.get("title"), r.get("duration_minutes") or 0) for r in rows),
        key=lambda kv: kv[1],
        default=(None, 0),
    )
    return {
        "total_meetings": len(rows),
        "total_minutes": sum(durs) if durs else None,
        "busiest_day": busiest_day,
        "longest_meeting": f"{longest[0]} ({longest[1]} min)" if longest[0] and longest[1] else None,
    }


def _aggregate_tasks(rows: list[dict]) -> dict:
    if not rows:
        return {}
    by_pri = Counter(r.get("priority") for r in rows)
    return {
        "total_unique": len({r["title"] for r in rows if r.get("title")}),
        "p1_total": by_pri.get(1, 0),
        "p2_total": by_pri.get(2, 0),
        "p3_total": by_pri.get(3, 0),
    }


def _compute_trends(curr_start: date, curr_end: date, garmin_now: dict) -> dict:
    """Compare current week vs previous week for select metrics (if previous data exists)."""
    prev_start = curr_start - timedelta(days=7)
    prev_end = curr_end - timedelta(days=7)
    rows = _fetch_range("garmin_metrics", "date, hrv, sleep_score, rhr, total_steps, distance_km", prev_start, prev_end)
    if not rows:
        return {}
    trends = {}
    for field, key in [("hrv", "hrv_vs_prev_week"), ("sleep_score", "sleep_vs_prev_week"),
                       ("rhr", "rhr_vs_prev_week")]:
        prev_vals = [r[field] for r in rows if r.get(field) is not None]
        curr_vals = garmin_now.get(f"mean_{field}")
        if prev_vals and curr_vals is not None:
            prev_avg = statistics.mean(prev_vals)
            delta = round(curr_vals - prev_avg, 1)
            sign = "+" if delta >= 0 else ""
            label = {"hrv": "HRV (мс, mean)", "sleep_score": "Sleep Score (mean)",
                     "rhr": "Пульс покоя (mean)"}[field]
            trends[key] = f"{sign}{delta} ({label}: было {round(prev_avg,1)} → стало {curr_vals})"

    # Steps / distance trends
    for field, key in [("total_steps", "steps_vs_prev_week"),
                       ("distance_km", "distance_vs_prev_week")]:
        prev_vals = [r[field] for r in rows if r.get(field) is not None]
        if field == "total_steps":
            curr_vals = garmin_now.get("sum_steps")
            curr_label = "Σ шаги за неделю"
        else:
            curr_vals = garmin_now.get("sum_distance_km")
            curr_label = "Σ км за неделю"
        if prev_vals and curr_vals is not None:
            prev_avg = sum(prev_vals)
            delta = round(curr_vals - prev_avg, 1)
            sign = "+" if delta >= 0 else ""
            unit = "шагов" if field == "total_steps" else "км"
            trends[key] = f"{sign}{delta} ({curr_label}: было {round(prev_avg,1)} → стало {curr_vals} {unit})"
    return trends


def build_facts(monday: date, sunday: date) -> dict:
    """Top-level pipeline: collect & aggregate all weekly facts."""
    garmin_rows = _fetch_range("garmin_metrics", GARMIN_COLS, monday, sunday)
    food_rows = _fetch_range("food_log", FOOD_COLS, monday, sunday)
    cal_rows = _fetch_range("calendar_events", CAL_COLS, monday, sunday)
    task_rows = _fetch_range("tasks", TASK_COLS, monday, sunday)

    garmin_w = _aggregate_garmin(garmin_rows)
    food_w = _aggregate_food(food_rows)
    cal_w = _aggregate_calendar(cal_rows)
    task_w = _aggregate_tasks(task_rows)
    trends = _compute_trends(monday, sunday, garmin_w)

    days_with = garmin_w.get("days", 0) if garmin_w else 0

    return {
        "week_range": f"Mon {monday} → Sun {sunday}",
        "request_date": date.today().isoformat(),
        "days_with_data": str(days_with),
        "garmin_weekly": garmin_w,
        "food_weekly": food_w,
        "calendar_weekly": cal_w,
        "tasks_weekly": task_w,
        "trends": trends,
        "_meta_rows": {
            "garmin": len(garmin_rows), "food": len(food_rows),
            "calendar": len(cal_rows), "tasks": len(task_rows),
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Telegram sender (direct Bot API, no Hermes)
# ─────────────────────────────────────────────────────────────────────

def _send_telegram(text: str, *, parse_mode: str = "Markdown") -> bool:
    """Send a single message via Telegram Bot API. Returns True on 200.

    On 400 chat-not-found the bot's token doesn't see this chat_id
    (user hasn't /start'd the bot, or @banano001_bot is a userbot).
    In that case we do NOT mark the job failed — we save the payload to
    /tmp/weekly_recap_last_sent.txt so the user can forward it manually,
    then return False so the caller's logs reflect "delivery skipped".
    """
    # Read chat_id fresh from .env every call — bypasses os.environ.setdefault
    # inertia (other modules in same process may have loaded a stale value).
    # This is defensive against cases where another bot had previously loaded
    # an old chat_id (e.g. 111251302 Morphius — forbidden since 06.07).
    env_path = Path("/root/morning_brief_v2/.env")
    env: dict[str, str] = {}
    for ln in env_path.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        env[k] = v
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                log.info(f"Telegram send OK: {len(text)} chars")
                return True
            log.error(f"Telegram send FAIL: status={resp.status} body={body[:300]}")
            return _stash_failed_send(text, chat_id, f"status={resp.status}: {body[:200]}")
    except urllib.error.HTTPError as e:
        # 400 chat-not-found is treated as soft failure: stash payload, return False.
        err_body = e.read().decode("utf-8", errors="replace")
        log.warning(f"Telegram send HTTPError {e.code} (delivery skipped): {err_body[:200]}")
        return _stash_failed_send(text, chat_id, f"HTTP {e.code}: {err_body[:200]}")
    except urllib.error.URLError as e:
        log.error(f"Telegram send network error: {e}")
        return _stash_failed_send(text, chat_id, f"URLError: {e}")
    except Exception as e:
        log.error(f"Telegram send unexpected error: {type(e).__name__}: {e}")
        return _stash_failed_send(text, chat_id, f"{type(e).__name__}: {e}")


def _stash_failed_send(text: str, chat_id: str, error: str) -> bool:
    """When Telegram delivery can't happen right now, save the message for later.

    Returns False (matches earlier contract), but does NOT raise — and does
    NOT mark the surrounding job as failed in Supabase. Caller decides what to
    do with the False.
    """
    try:
        stash_path = Path("/tmp/weekly_recap_last_sent.txt")
        stash_path.write_text(
            f"# Telegram delivery skipped\n"
            f"# chat_id: {chat_id}\n"
            f"# error:   {error}\n"
            f"# when:    {os.environ.get('TZ', '') or ''}{datetime.now().isoformat()}\n"
            f"# to forward manually: paste the block below into the chat.\n"
            f"\n{text}\n",
            encoding="utf-8",
        )
        log.info(f"Stashed skipped message to {stash_path} (len={len(text)} chars)")
    except Exception as e:
        log.error(f"Failed to stash message: {e}")
    return False


# ─────────────────────────────────────────────────────────────────────
# Deterministic fallback when LLM disabled
# ─────────────────────────────────────────────────────────────────────

def _deterministic_recap(facts: dict, out: dict | None = None) -> str:
    """Build a simple recap without LLM.

    If `out` (5-field dict from LLM) is given, use it for tone sections;
    otherwise emit a number-only bullet list.
    """
    lines = [
        f"📊 *Weekly Recap — {facts['week_range']}*",
        f"_Дней с данными: {facts['days_with_data']}/7_",
        "",
    ]
    if out:
        lines.append(f"*{out.get('headline', '—')}*")
        lines.append("")
        for label, key in [("🛌 Сон", "sleep"), ("💼 Работа", "work"),
                           ("🍽 Питание", "nutrition"), ("🎯 Рекомендации", "next_week")]:
            lines.append(f"*{label}*\n{out.get(key, '—')}")
            lines.append("")
    else:
        g = facts.get("garmin_weekly") or {}
        f = facts.get("food_weekly") or {}
        c = facts.get("calendar_weekly") or {}
        t = facts.get("tasks_weekly") or {}
        if g:
            lines.append(f"🛌 Сон: {g.get('mean_sleep_min')} мин (avg), HRV {g.get('mean_hrv')}, "
                         f"BB {g.get('mean_body_battery')}, Sleep Score {g.get('mean_sleep_score')}")
        if f:
            lines.append(f"🍽 Питание: {f.get('mean_kcal_per_day')} ккал/день, "
                         f"белок {f.get('mean_protein_g')} g/день ({f.get('log_days')}/7 дней)")
        if c:
            lines.append(f"📅 Встречи: {c.get('total_meetings')} шт, "
                         f"{c.get('total_minutes')} мин, busy {c.get('busiest_day')}")
        if t:
            lines.append(f"✅ Задачи: {t.get('total_unique')} уникальных, "
                         f"P1 {t.get('p1_total')}, P2 {t.get('p2_total')}, P3 {t.get('p3_total')}")
        if facts.get("trends"):
            lines.append("")
            lines.append("*Динамика vs прошлая:*")
            for k, v in facts["trends"].items():
                lines.append(f"  - {v}")
    lines.append("")
    lines.append("— morning_brief_v2 · weekly-recap")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="compute facts + show, do NOT call LLM, do NOT send")
    p.add_argument("--no-llm", action="store_true",
                   help="deterministic recap (no LLM call), still send to TG")
    p.add_argument("--week-of", type=str, default=None,
                   help="week containing this date (YYYY-MM-DD); default: last completed week")
    p.add_argument("--send-only", type=str, default=None,
                   help="read cached 5-field JSON from file, format & send")
    p.add_argument("--out-file", type=str, default=None,
                   help="write facts dict to JSON (for debugging/recovery)")
    args = p.parse_args()

    # Mode: send-only (re-send a previously composed narrative)
    if args.send_only:
        path = Path(args.send_only)
        payload = json.loads(path.read_text())
        out = payload.get("narrative")
        week_range = payload.get("week_range", "unknown")
        if not out:
            log.error(f"--send-only: no 'narrative' key in {path}")
            return 2
        facts_cached = payload.get("facts")
        pages = render_for_telegram_pages(out, week_range=week_range, facts=facts_cached)
        log.info(f"Sending {len(pages)} cached page(s) for {week_range}")
        delivered = 0
        skipped = 0
        for page in pages:
            if _send_telegram(page):
                delivered += 1
            else:
                skipped += 1
        if delivered == 0 and skipped > 0:
            log.warning(f"All {skipped} cached page(s) delivery skipped. "
                        f"Re-use --send-only {path} once chat_id is fixed.")
        return 0

    # Compute week range
    if args.week_of:
        d = date.fromisoformat(args.week_of)
        monday, sunday = _week_of(d)
    else:
        monday, sunday = _last_completed_week()
    log.info(f"Week range: {monday} → {sunday}")

    facts = build_facts(monday, sunday)
    log.info(f"Rows fetched: {facts.pop('_meta_rows')}")

    if args.out_file:
        Path(args.out_file).write_text(json.dumps(facts, indent=2, default=str, ensure_ascii=False))
        log.info(f"Facts written to {args.out_file}")

    if args.dry_run:
        print(json.dumps({k: v for k, v in facts.items() if not k.startswith("_")},
                         indent=2, ensure_ascii=False, default=str))
        return 0

    out: dict | None = None
    if not args.no_llm:
        out = compose_narrative(facts)
        if out is None:
            log.warning("LLM call returned None — falling back to deterministic recap")

    text = _deterministic_recap(facts, out)
    pages = (render_for_telegram_pages(out, week_range=facts["week_range"], facts=facts)
             if out else [text])
    # If deterministic, `pages` may be 1 entry; otherwise send all
    if not out:
        pages = [text]

    log.info(f"Total pages to send: {len(pages)} (chars: {[len(p) for p in pages]})")
    delivered = 0
    skipped = 0
    for i, page in enumerate(pages, 1):
        log.info(f"Sending page {i}/{len(pages)}")
        if _send_telegram(page):
            delivered += 1
        else:
            skipped += 1

    # Always cache — works for both LLM (out is dict) and --no-llm (out is None
    # but facts+text are still there for manual re-send or forward).
    cache_path = Path("/tmp/weekly_recap_last.json")
    cache_payload = {
        "week_range": facts["week_range"],
        "facts": facts,
        "narrative": out,  # dict if LLM ran, None if --no-llm or LLM failed
        "deterministic_text": text,  # always present — usable for manual forward
        "delivery": {"delivered": delivered, "skipped": skipped,
                     "stashed_to": "/tmp/weekly_recap_last_sent.txt" if skipped else None},
    }
    cache_path.write_text(json.dumps(cache_payload, indent=2, ensure_ascii=False, default=str))
    log.info(f"Cached composed narrative to {cache_path}")

    # Exit status: only hard-fail if there were no pages to send at all.
    # Soft-fail (TG delivery skipped) is reported but does NOT mark job failed.
    if delivered == 0 and skipped > 0:
        log.warning(f"All {skipped} page(s) delivery skipped (chat_not_found or network). "
                    f"Use --send-only /tmp/weekly_recap_last.json to retry later.")
        return 0  # LLM done, narrative cached, manual re-send possible
    return 0


if __name__ == "__main__":
    sys.exit(main())
