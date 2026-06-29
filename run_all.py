#!/usr/bin/env python3
"""Run ALL data providers for a date and render the morning brief HTML.

Pipeline:
    HelioProvider   -> upsert_helio_metrics
    GarminProvider  -> upsert_garmin_metrics   (already covered by run_garmin.py)
    FoodProvider    -> upsert_food_log         (food_date = target - 1 day)
    WeatherProvider -> upsert_weather_log
    CalendarProvider-> upsert_calendar_events
    TodoistProvider -> upsert_tasks

After all providers finish -> render_playful.render_live(target, out_path)

Usage:
    cd /root/morning_brief_v2 && set -a && source .env && set +a && \\
        .venv/bin/python run_all.py                    # today
        .venv/bin/python run_all.py --date 2026-06-27  # specific day
        .venv/bin/python run_all.py --skip-garmin      # if already ran run_garmin.py

Idempotent: all upserts use on_conflict=date (garmin/helio) or delete-then-insert
(food/weather/calendar/tasks). Re-running is safe.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "/root/morning_brief_v2")

from db.client import (  # noqa: E402
    upsert_brief, upsert_garmin_metrics, upsert_helio_metrics,
    upsert_food_log, upsert_weather_log, upsert_calendar_events, upsert_tasks,
)
from providers.garmin import GarminProvider    # noqa: E402
# HelioProvider disabled 2026-06-29 — браслет больше не носим, данные не нужны.
# from providers.helio import HelioProvider      # noqa: E402
from providers.food import FoodProvider        # noqa: E402
from providers.weather import WeatherProvider  # noqa: E402
from providers.calendar import CalendarProvider  # noqa: E402
from providers.todoist import TodoistProvider  # noqa: E402
from playful.render_playful import render_live  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_all")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run all providers and render morning brief")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Brief date (default: today)")
    p.add_argument("--out", default="/tmp/brief_playful.html", help="Output HTML path")
    p.add_argument("--skip-garmin", action="store_true",
                   help="Skip Garmin (use existing garmin_metrics row)")
    p.add_argument("--skip-helio", action="store_true", help="Skip Helio")
    p.add_argument("--skip-food", action="store_true", help="Skip Food log")
    p.add_argument("--skip-weather", action="store_true", help="Skip Weather")
    p.add_argument("--skip-calendar", action="store_true", help="Skip Calendar")
    p.add_argument("--skip-tasks", action="store_true", help="Skip Tasks/Todoist")
    p.add_argument("--no-render", action="store_true",
                   help="Don't render HTML, only collect data")
    return p.parse_args()


async def _run_provider(provider, name: str, target_date: date) -> tuple[str, object | None, str | None]:
    """Run a single provider, return (name, data, error).

    Forwards target_date to providers that accept it (Garmin).
    Providers without date support (Food, Weather, Calendar, Tasks, Helio)
    ignore the arg.
    """
    try:
        # GarminProvider.fetch accepts target_date; others don't.
        if hasattr(provider, "fetch") and "target_date" in provider.fetch.__code__.co_varnames:
            result = await provider.fetch(target_date=target_date)
        else:
            result = await provider.fetch()
        if result.status == "unavailable" or not result.data:
            logger.warning("[%s] unavailable: %s", name, result.error)
            return name, None, result.error
        # Log a fingerprint so we can spot if data is mixed up between days.
        if isinstance(result.data, dict):
            bb   = result.data.get("body_battery")
            kcal = result.data.get("resting_kcal")
            rhr  = result.data.get("rhr")
            sl   = result.data.get("sleep_duration_min")
            logger.info("[%s] data fingerprint: bb=%s kcal=%s rhr=%s sleep=%s",
                        name, bb, kcal, rhr, sl)
        logger.info("[%s] ok, %d keys", name, len(result.data) if isinstance(result.data, dict) else 0)
        return name, result.data, None
    except Exception as e:
        logger.exception("[%s] crashed: %s", name, e)
        return name, None, str(e)


def _write_provider(brief_id: str, target: date, name: str, data) -> bool:
    """Write provider data into its Supabase table. Returns True on success.

    Each provider returns a dict from fetch(); we extract the list payload
    by provider name. Empty/missing lists degrade gracefully (0 rows).
    """
    try:
        if name == "garmin":
            upsert_garmin_metrics(brief_id, target.isoformat(), data)
        elif name == "helio":
            upsert_helio_metrics(brief_id, target.isoformat(), data)
        elif name == "food":
            entries = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            upsert_food_log(brief_id, (target - timedelta(days=1)).isoformat(), entries)
        elif name == "weather":
            periods = data.get("periods", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            upsert_weather_log(brief_id, target.isoformat(), periods)
        elif name == "calendar":
            events = data.get("events", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            upsert_calendar_events(brief_id, target.isoformat(), events)
        elif name == "tasks":
            tasks = data.get("tasks", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            upsert_tasks(brief_id, target.isoformat(), tasks)
        else:
            logger.warning("[%s] no writer registered, skipped", name)
            return False
        logger.info("[%s] written to Supabase", name)
        return True
    except Exception as e:
        logger.exception("[%s] DB write failed: %s", name, e)
        return False


async def main() -> int:
    args = parse_args()
    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else date.today())
    logger.info("Target date: %s", target.isoformat())

    # Step 1: ensure brief row exists (some providers need brief_id as FK)
    brief = upsert_brief(target.isoformat())
    brief_id = (brief or {}).get("id")
    if not brief_id:
        logger.error("upsert_brief returned no id, aborting")
        return 3
    logger.info("Brief upserted: id=%s", brief_id)

    # Step 2: run all providers in parallel
    skip = {n for n, flag in (
        ("garmin", args.skip_garmin), ("helio", args.skip_helio),
        ("food", args.skip_food), ("weather", args.skip_weather),
        ("calendar", args.skip_calendar), ("tasks", args.skip_tasks),
    ) if flag}

    provider_factories = {
        "garmin":   GarminProvider,
        # "helio":    HelioProvider,  # disabled 2026-06-29 — браслет больше не носим
        "food":     FoodProvider,
        "weather":  WeatherProvider,
        "calendar": CalendarProvider,
        "tasks":    TodoistProvider,
    }

    tasks = []
    for name, factory in provider_factories.items():
        if name in skip:
            logger.info("[%s] skipped (flag)", name)
            continue
        tasks.append(_run_provider(factory(), name, target))

    results = await asyncio.gather(*tasks)
    for name, data, err in results:
        if data is not None:
            _write_provider(brief_id, target, name, data)

    # Step 3: render
    if args.no_render:
        logger.info("--no-render set, skipping render_playful")
        return 0

    try:
        out = render_live(target, args.out)
        logger.info("[render] HTML -> %s", out)
        return 0
    except Exception as e:
        logger.exception("render_live failed: %s", e)
        return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))