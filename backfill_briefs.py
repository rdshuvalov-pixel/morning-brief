"""Backfill morning briefs for a date range.

Reads existing provider data from *Metrics tables and generates
narrative + renders HTML without re-fetching from providers.

Usage:
    python backfill_briefs.py 2026-06-10 2026-06-17
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from models import (
    BriefContext, CalendarEvent, DayStatus, GarminData, HelioData,
    FoodEntry, TaskEntry, WeatherEntry,
)
from db.client import get_client
from narrator import generate
from renderer import render_html, render_telegram
from scorer import Scorer


GARMIN_FIELDS = {
    'sleep_duration_min', 'sleep_score', 'deep_sleep_pct', 'hrv',
    'body_battery', 'rhr', 'spo2', 'training_readiness', 'stress', 'skin_temp',
}
HELIO_FIELDS = {
    'readiness', 'physical', 'mental', 'hrv_score', 'sleep_hrv',
    'rhr', 'steps', 'kcal',
}


def get_garmin_by_date(sb, date_val: str) -> dict | None:
    result = sb.table("garmin_metrics").select("*").eq("date", date_val).maybe_single().execute()
    return (result.data or None) if hasattr(result, 'data') else None


def get_helio_by_date(sb, date_val: str) -> dict | None:
    result = sb.table("helio_metrics").select("*").eq("date", date_val).maybe_single().execute()
    return (result.data or None) if hasattr(result, 'data') else None


def get_food_by_date(sb, date_val: str) -> list[dict]:
    result = sb.table("food_log").select("*").eq("date", date_val).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_weather_by_date(sb, date_val: str) -> list[dict]:
    result = sb.table("weather_log").select("*").eq("date", date_val).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_tasks_by_date(sb, date_val: str) -> list[dict]:
    result = sb.table("tasks").select("*").eq("date", date_val).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def build_context(sb, brief_id: str, date_val: date) -> BriefContext:
    d_str = date_val.isoformat()

    g_raw = get_garmin_by_date(sb, d_str)
    h_raw = get_helio_by_date(sb, d_str)
    food_rows = get_food_by_date(sb, d_str)
    weather_rows = get_weather_by_date(sb, d_str)
    task_rows = get_tasks_by_date(sb, d_str)

    garmin = GarminData(**{k: v for k, v in g_raw.items() if k in GARMIN_FIELDS}) if g_raw else None
    helio = HelioData(**{k: v for k, v in h_raw.items() if k in HELIO_FIELDS}) if h_raw else None

    food = [FoodEntry(**r) for r in food_rows]
    weather = [WeatherEntry(**r) for r in weather_rows]
    tasks = [TaskEntry(title=t["title"], priority=t.get("priority")) for t in task_rows]

    return BriefContext(
        brief_id=brief_id,
        date=date_val,
        garmin=garmin,
        helio=helio,
        food=food,
        weather=weather,
        calendar=[],  # calendar not stored in metrics tables
        tasks=tasks,
    )


def backfill_date(sb, date_val: date) -> bool:
    d_str = date_val.isoformat()
    print(f"\n{'='*50}")
    print(f"Processing {d_str}")

    # Get existing brief record
    brief_resp = sb.table("briefs").select("id").eq("date", d_str).execute()
    if not brief_resp.data:
        print(f"  No brief record for {d_str}, skipping")
        return False

    brief_id = brief_resp.data[0]["id"]
    print(f"  brief_id: {brief_id}")

    # Build context from DB
    ctx = build_context(sb, brief_id, date_val)

    g = ctx.garmin
    h = ctx.helio
    print(f"  Garmin: sleep={g.sleep_duration_min if g else None}min, "
          f"hrv={g.hrv if g else None}, "
          f"deep_sleep={g.deep_sleep_pct if g else None}%")
    print(f"  Helio: readiness={h.readiness if h else None}, "
          f"hrv_score={h.hrv_score if h else None}")

    # Score
    status = Scorer().score(ctx)
    print(f"  Status: {status.status}")

    # Generate narrative
    narrative, narrative_source = generate(ctx, status)
    print(f"  Narrative source: {narrative_source}")
    print(f"  Narrative preview: {narrative[:200]}...")

    # Render
    html = render_html(narrative, ctx, status)
    tg_text = render_telegram(narrative, status, None)

    # Update briefs record
    update_cols = {
        "telegram_text": tg_text,
        "status": status.status,
        "narrative": narrative,
        # brief_url intentionally left blank — Vercel deploy broken
    }
    sb.table("briefs").update(update_cols).eq("id", brief_id).execute()
    print(f"  Updated briefs record")

    return True


def main():
    if len(sys.argv) < 3:
        print("Usage: python backfill_briefs.py <start_date> <end_date>")
        print("Example: python backfill_briefs.py 2026-06-10 2026-06-17")
        sys.exit(1)

    start_date = date.fromisoformat(sys.argv[1])
    end_date = date.fromisoformat(sys.argv[2])

    if start_date > end_date:
        print("Error: start_date must be <= end_date")
        sys.exit(1)

    sb = get_client()

    current = start_date
    count = 0
    while current <= end_date:
        if backfill_date(sb, current):
            count += 1
        current += timedelta(days=1)

    print(f"\nDone. Processed {count} briefs.")


if __name__ == "__main__":
    main()
