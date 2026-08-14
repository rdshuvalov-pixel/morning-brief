#!/usr/bin/env python3
"""One-shot runner: refresh Garmin metrics for TODAY into garmin_metrics.

User decision (2026-07-08): write ALL fields Garmin Connect returns for the
current date — including the morning settled-numbers (BB peak, HRV overnight,
sleep duration/score/deep_pct, RHR, SpO2, training_readiness, stress) plus the
intraday live-fields (total_steps, distance_km, resting_kcal, active_kcal).

Why this differs from run_garmin.py:
    `run_garmin.py` drops BB/HRV/sleep_*/deep_sleep_pct when target_date is
    unclosed (today) because the Garmin API echoes back yesterday's settled
    numbers as a courtesy. That drop is the right behaviour for a cron that
    runs once a day on yesterday, but WRONG for an ad-hoc "fetch today's
    morning numbers" — the user wants the live API response, even if some
    fields happen to overlap with yesterday's settled values.

    `run_garmin_today.py` skips the settled-drop step entirely. Whatever
    Garmin returns for date=today is written verbatim.

Usage:
    cd /root/morning_brief_v2 && set -a && source .env && set +a && \\
        .venv/bin/python run_garmin_today.py
        .venv/bin/python run_garmin_today.py --date 2026-07-08   # override (rare)

Idempotent via on_conflict='date' on both briefs and garmin_metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime

sys.path.insert(0, "/root/morning_brief_v2")

from db.client import (  # noqa: E402
    upsert_brief,
    upsert_garmin_metrics,
    upsert_garmin_sleep_minutes,
)
from providers.garmin import GarminProvider                 # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("garmin_today_runner")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Refresh Garmin metrics for TODAY (no settled-drop).",
    )
    p.add_argument(
        "--date",
        dest="target_date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Override target date (default: today). Rare — only if you want "
             "to re-fetch an unclosed day after a partial run.",
    )
    return p.parse_args()


def resolve_target(args: argparse.Namespace) -> date:
    if args.target_date:
        try:
            return datetime.strptime(args.target_date, "%Y-%m-%d").date()
        except ValueError:
            logger.error("Invalid --date %r (expected YYYY-MM-DD), aborting",
                         args.target_date)
            sys.exit(2)
    return date.today()


async def run_for_today(target: date, provider: GarminProvider) -> int:
    t_str = target.isoformat()
    logger.info("=== garmin_today %s ===", t_str)

    result = await provider.fetch(target_date=target)
    logger.info("GarminProvider status=%s error=%s", result.status, result.error)
    if result.status == "unavailable" or not result.data:
        logger.error("Garmin data unavailable for %s, skipping DB write", t_str)
        return 2

    metrics = result.data
    logger.info(
        "Garmin metrics fetched (no settled-drop): "
        "body_battery=%s hrv=%s rhr=%s sleep=%smin deep=%s%% tr=%s "
        "spo2=%s stress=%s steps=%s dist=%skm",
        metrics.get("body_battery"),
        metrics.get("hrv"),
        metrics.get("rhr"),
        metrics.get("sleep_duration_min"),
        metrics.get("deep_sleep_pct"),
        metrics.get("training_readiness"),
        metrics.get("spo2"),
        metrics.get("stress"),
        metrics.get("total_steps"),
        metrics.get("distance_km"),
    )

    # NOTE: NO settled-drop here. Whatever Garmin returned for date=today
    # is written verbatim. This is the deliberate user override (2026-07-08)
    # against run_garmin.py's settled-drop behaviour.

    brief = upsert_brief(t_str)
    brief_id = brief.get("id") if brief else None
    if not brief_id:
        logger.error("upsert_brief returned no id for %s, aborting", t_str)
        return 3
    logger.info("Brief upserted: id=%s date=%s", brief_id, t_str)

    # Поминутную развертку вынимаем ДО upsert агрегатов: она предназначена для
    # garmin_sleep_minutes, и в payload garmin_metrics попадать не должна.
    minute_rows = metrics.pop("sleep_minute_rows", []) or []
    metrics.pop("sleep_minute_count", None)

    garmin_row = upsert_garmin_metrics(brief_id, t_str, metrics)
    logger.info(
        "garmin_metrics upserted (today, all fields): id=%s "
        "body_battery=%s hrv=%s sleep=%smin",
        garmin_row.get("id"),
        garmin_row.get("body_battery"),
        garmin_row.get("hrv"),
        garmin_row.get("sleep_duration_min"),
    )

    # Поминутная развертка сна — отдельная таблица garmin_sleep_minutes.
    if minute_rows:
        try:
            inserted = upsert_garmin_sleep_minutes(t_str, minute_rows)
            logger.info(
                "garmin_sleep_minutes upserted: %d rows for %s",
                len(inserted), t_str,
            )
        except Exception as e:
            logger.warning(
                "garmin_sleep_minutes upsert failed for %s: %s — "
                "if table does not exist, apply migration 009 first",
                t_str, str(e)[:200],
            )
    return 0


async def main() -> int:
    args = parse_args()
    target = resolve_target(args)
    logger.info("Target date: %s", target.isoformat())

    provider = GarminProvider()
    return await run_for_today(target, provider)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))