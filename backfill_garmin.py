#!/usr/bin/env python3
"""Backfill Garmin metrics into Supabase for a date range.

Usage:
    cd /root/morning_brief_v2
    source .env
    .venv/bin/python backfill_garmin.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, timedelta

# ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from garminconnect import Garmin

from config import GARMIN_EMAIL, GARMIN_PASSWORD
from db.client import get_client, upsert_brief, upsert_garmin_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

START_DATE = date(2026, 5, 1)
END_DATE   = date(2026, 6, 17)


def garmin_login() -> Garmin:
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login(tokenstore="/root/.garminconnect")
    return client


def fetch_sleep(client: Garmin, date_str: str) -> dict:
    try:
        sleep = client.get_sleep_data(date_str)
        if not sleep:
            return {}

        dto = sleep.get("dailySleepDTO", {}) if sleep else {}
        deep = dto.get("deepSleepSeconds", 0) or 0
        light = dto.get("lightSleepSeconds", 0) or 0
        rem = dto.get("remSleepSeconds", 0) or 0
        awake = dto.get("awakeSleepSeconds", 0) or 0
        total_sec = deep + light + rem + awake
        sleep_duration_min = total_sec // 60 if total_sec else None

        sleep_score = None
        if isinstance(sleep.get("sleepScore"), dict):
            sleep_score = sleep["sleepScore"].get("overall")

        deep_sleep_pct = None
        if total_sec:
            deep_sleep_pct = round(deep / total_sec * 100, 1)

        return {
            "sleep_duration_min": sleep_duration_min,
            "sleep_score": sleep_score,
            "deep_sleep_pct": deep_sleep_pct,
        }
    except Exception as e:
        logger.warning("Sleep fetch error for %s: %s", date_str, e)
        return {}


def fetch_hrv(client: Garmin, date_str: str) -> dict:
    try:
        hrv = client.get_hrv_data(date_str)
        if not hrv:
            return {}
        last_night = None
        if isinstance(hrv.get("hrvSummary"), dict):
            last_night = hrv["hrvSummary"].get("lastNightAvg")
        return {"hrv": last_night}
    except Exception as e:
        logger.warning("HRV fetch error for %s: %s", date_str, e)
        return {}


def fetch_daily_stats(client: Garmin, date_str: str) -> dict:
    try:
        daily = client.get_user_summary(date_str)
        if not daily:
            return {}

        d = daily

        rhr = d.get("restingHeartRate")

        # body battery via dedicated endpoint
        body_battery = None
        bb_data = client.get_body_battery(date_str)
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            body_battery = bb_data[0].get("charged")

        # training readiness
        training_readiness = None
        tr_data = client.get_training_readiness(date_str)
        if tr_data and isinstance(tr_data, list) and len(tr_data) > 0:
            training_readiness = tr_data[0].get("score")

        spo2 = d.get("averageSpo2")

        stress = d.get("averageStressLevel")
        skin_temp = d.get("averageSkinTempDeviation")

        resting_kcal = d.get("bmrKilocalories")
        active_kcal  = d.get("activeKilocalories")

        return {
            "rhr": rhr,
            "body_battery": body_battery,
            "spo2": spo2,
            "training_readiness": training_readiness,
            "stress": stress,
            "skin_temp": skin_temp,
            "resting_kcal": resting_kcal,
            "active_kcal": active_kcal,
        }
    except Exception as e:
        logger.warning("Daily stats fetch error for %s: %s", date_str, e)
        return {}


def get_or_create_brief(date_str: str) -> str | None:
    sb = get_client()
    existing = sb.table("briefs").select("id").eq("date", date_str).maybe_single().execute()
    data = existing.data if hasattr(existing, "data") else existing
    if data:
        return data["id"]

    # create brief
    result = upsert_brief(date_str)
    if isinstance(result, list) and result:
        return result[0].get("id") if isinstance(result[0], dict) else None
    if isinstance(result, dict):
        return result.get("id")
    return None


def backfill_date(client: Garmin, d: date) -> bool:
    date_str = d.isoformat()
    logger.info("Processing %s", date_str)

    # fetch Garmin data
    sleep_data = fetch_sleep(client, date_str)
    time.sleep(3)

    hrv_data = fetch_hrv(client, date_str)
    time.sleep(3)

    daily_data = fetch_daily_stats(client, date_str)
    time.sleep(3)

    metrics = {**sleep_data, **hrv_data, **daily_data}
    if not metrics:
        logger.warning("No metrics fetched for %s, skipping", date_str)
        return False

    # get or create brief
    brief_id = get_or_create_brief(date_str)
    if not brief_id:
        logger.error("Failed to get or create brief for %s", date_str)
        return False

    # upsert garmin_metrics
    upsert_garmin_metrics(brief_id, date_str, metrics)
    logger.info("Stored garmin_metrics for %s (brief_id=%s)", date_str, brief_id)
    return True


def main():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        logger.error("GARMIN_EMAIL / GARMIN_PASSWORD not set in environment")
        sys.exit(1)

    logger.info("Logging into Garmin...")
    client = garmin_login()
    logger.info("Logged in successfully")

    current = START_DATE
    success = 0
    failed = 0

    while current <= END_DATE:
        try:
            ok = backfill_date(client, current)
            if ok:
                success += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Unhandled error for %s: %s", current, e)
            failed += 1

        current += timedelta(days=1)
        time.sleep(3)  # rate limit between dates

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    main()
