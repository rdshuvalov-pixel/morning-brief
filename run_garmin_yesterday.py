#!/usr/bin/env python3
"""One-shot runner: refresh Garmin metrics for yesterday (idempotent upsert).

Usage:
    cd /root/morning_brief_v2 && set -a && source .env && set +a && \
        .venv/bin/python run_garmin_yesterday.py

Why this exists: morning_brief_v2/providers/garmin.py is a pure provider
(no DB writes). This runner wires it to db.client.upsert_garmin_metrics
using on_conflict='date' (so re-running is safe).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "/root/morning_brief_v2")

from db.client import upsert_brief, upsert_garmin_metrics  # noqa: E402
from providers.garmin import GarminProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("garmin_runner")


async def main() -> int:
    yesterday = date.today() - timedelta(days=1)
    y_str = yesterday.isoformat()
    logger.info("Target date (yesterday): %s", y_str)

    provider = GarminProvider()
    result = await provider.fetch()

    logger.info("GarminProvider status=%s error=%s", result.status, result.error)
    if result.status == "unavailable" or not result.data:
        logger.error("Garmin data unavailable, aborting before any DB write")
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

    brief = upsert_brief(y_str)
    brief_id = brief.get("id") if brief else None
    if not brief_id:
        logger.error("upsert_brief returned no id, aborting")
        return 3
    logger.info("Brief upserted: id=%s date=%s", brief_id, y_str)

    garmin_row = upsert_garmin_metrics(brief_id, y_str, metrics)
    logger.info("garmin_metrics upserted: id=%s body_battery=%s",
                garmin_row.get("id"), garmin_row.get("body_battery"))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))