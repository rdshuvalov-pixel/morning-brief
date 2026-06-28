#!/usr/bin/env python3
"""One-shot runner: refresh Garmin metrics into garmin_metrics (idempotent upsert).

Default target = TODAY (not yesterday). This used to default to yesterday,
which silently dropped every morning's data — the user was already awake by
07:00 with sleep/HRV/RHR/Body Battery available, but the runner wouldn't
collect them until the day "closed" the next morning.

Usage:
    cd /root/morning_brief_v2 && set -a && source .env && set +a && \\
        .venv/bin/python run_garmin.py                    # today
        .venv/bin/python run_garmin.py --date 2026-06-27  # specific day
        .venv/bin/python run_garmin.py --date 2026-06-27 --date 2026-06-28  # batch

Idempotent via on_conflict='date' on both briefs and garmin_metrics — re-running
for the same date overwrites the row instead of duplicating.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "/root/morning_brief_v2")

from db.client import upsert_brief, upsert_garmin_metrics  # noqa: E402
from providers.garmin import GarminProvider                 # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("garmin_runner")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh Garmin metrics into Supabase")
    p.add_argument(
        "--date",
        action="append",
        dest="dates",
        metavar="YYYY-MM-DD",
        help="Target date (repeatable). Default: today.",
    )
    return p.parse_args()


def resolve_dates(args: argparse.Namespace) -> list[date]:
    if not args.dates:
        return [date.today()]
    out: list[date] = []
    for s in args.dates:
        try:
            out.append(datetime.strptime(s, "%Y-%m-%d").date())
        except ValueError:
            logger.error("Invalid --date %r (expected YYYY-MM-DD), aborting", s)
            sys.exit(2)
    # de-dupe, preserve order
    seen, deduped = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            deduped.append(d)
    return deduped


async def run_for_date(target: date, provider: GarminProvider) -> int:
    t_str = target.isoformat()
    logger.info("=== %s ===", t_str)

    result = await provider.fetch(target_date=target)
    logger.info("GarminProvider status=%s error=%s", result.status, result.error)
    if result.status == "unavailable" or not result.data:
        logger.error("Garmin data unavailable for %s, skipping DB write", t_str)
        return 2

    metrics = result.data
    logger.info(
        "Garmin metrics fetched: body_battery=%s hrv=%s rhr=%s sleep=%smin deep=%s%% tr=%s",
        metrics.get("body_battery"),
        metrics.get("hrv"),
        metrics.get("rhr"),
        metrics.get("sleep_duration_min"),
        metrics.get("deep_sleep_pct"),
        metrics.get("training_readiness"),
    )

    brief = upsert_brief(t_str)
    brief_id = brief.get("id") if brief else None
    if not brief_id:
        logger.error("upsert_brief returned no id for %s, aborting", t_str)
        return 3
    logger.info("Brief upserted: id=%s date=%s", brief_id, t_str)

    garmin_row = upsert_garmin_metrics(brief_id, t_str, metrics)
    logger.info("garmin_metrics upserted: id=%s body_battery=%s",
                garmin_row.get("id"), garmin_row.get("body_battery"))
    return 0


async def main() -> int:
    args = parse_args()
    targets = resolve_dates(args)
    logger.info("Target dates: %s", [d.isoformat() for d in targets])

    provider = GarminProvider()
    worst = 0
    for t in targets:
        rc = await run_for_date(t, provider)
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))