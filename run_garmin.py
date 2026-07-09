#!/usr/bin/env python3
"""One-shot runner: refresh Garmin metrics into garmin_metrics (idempotent upsert).

Default behaviour as of 2026-07-03:
    Fetch and upsert BOTH today AND yesterday.
    - `garmin_metrics[date=today]`     → live data at fetch time (steps/kcal accumulate)
    - `garmin_metrics[date=yesterday]` → settlement data (sleep/HRV/RHR/BB/TR/stress
      become "settled" only the morning after — Garmin API returns final numbers
      the next day, not in real-time).

Why two rows: the morning brief renders the "movement" block from `garmin_yesterday`
(see playful/render_playful.py:676 `movement_src = garmin_yesterday or garmin`).
That lookup only works correctly if BOTH rows exist with the right `date` column.
Without this, `garmin_metrics[date=today]` carries settlement data from the
previous morning's cron run, but its `date` is set to the day before — so
`get_garmin_metrics(yesterday)` returns either the wrong row or nothing.

Usage:
    cd /root/morning_brief_v2 && set -a && source .env && set +a && \\
        .venv/bin/python run_garmin.py                         # both today + yesterday
        .venv/bin/python run_garmin.py --date 2026-07-02       # specific day only
        .venv/bin/python run_garmin.py --date 2026-07-02 --date 2026-07-01  # batch
        .venv/bin/python run_garmin.py --also-yesterday=false  # legacy single-date

Idempotent via on_conflict='date' on both briefs and garmin_metrics — re-running
for the same date overwrites the row instead of duplicating.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/root/morning_brief_v2")

from db.client import upsert_brief, upsert_garmin_metrics  # noqa: E402
from providers.garmin import GarminProvider                 # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("garmin_runner")

# Operator's local timezone (Europe/Lisbon). Garmin Connect's "today" is
# defined by the user's local clock, not UTC — when a Lisbon 00:30 cron
# runs, UTC is still 23:30 of the previous day, so date.today() would
# incorrectly classify the target as "yesterday" and skip settled-drop.
# See skill Pitfall §24a (TZ drift).
_LOCAL_TZ = ZoneInfo("Europe/Lisbon")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh Garmin metrics into Supabase")
    p.add_argument(
        "--date",
        action="append",
        dest="dates",
        metavar="YYYY-MM-DD",
        help="Target date (repeatable). Default: today.",
    )
    p.add_argument(
        "--also-yesterday",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also fetch and upsert yesterday (default: true). "
             "Use --no-also-yesterday for legacy single-date behaviour.",
    )
    return p.parse_args()


def resolve_dates(args: argparse.Namespace) -> list[date]:
    if not args.dates:
        today = date.today()
        out = [today]
        if args.also_yesterday:
            out.append(today - timedelta(days=1))
        return out
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
        "Garmin metrics fetched: body_battery=%s hrv=%s rhr=%s sleep=%smin deep=%s%% tr=%s steps=%s",
        metrics.get("body_battery"),
        metrics.get("hrv"),
        metrics.get("rhr"),
        metrics.get("sleep_duration_min"),
        metrics.get("deep_sleep_pct"),
        metrics.get("training_readiness"),
        metrics.get("total_steps"),
    )

    # Day-boundary split (live vs settled), 2026-07-06 fix.
    #
    # Garmin Connect for a target date that is *not yet closed* (today's date
    # when called mid-morning) returns the FULL payload — including settled
    # metrics like body_battery, sleep_duration_min, sleep_score, hrv — but
    # those values are really the previous day's settled numbers being
    # re-echoed by the API. Writing the whole payload to garmin_metrics[date=today]
    # therefore produced a row that claimed to be today's data but contained
    # yesterday's numbers — a silent duplicate, hard to spot because
    # `id`/`date` differ but the numbers do not.
    #
    # Fix: when target is *not closed* yet (i.e. target_date >= today in the
    # operator's local timezone), keep ONLY the fields that genuinely update
    # intraday. Everything else is settled and belongs to yesterday.
    today_local = datetime.now(_LOCAL_TZ).date()
    if target >= today_local:
        dropped = []
        for k in ("body_battery", "hrv",
                  "sleep_duration_min", "sleep_score", "deep_sleep_pct"):
            if k in metrics:
                metrics.pop(k)
                dropped.append(k)
        if dropped:
            logger.info(
                "target=%s is unclosed today -> dropped settled-only fields %s",
                t_str, dropped,
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