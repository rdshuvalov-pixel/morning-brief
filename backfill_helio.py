#!/usr/bin/env python3
"""Backfill Helio (Zepp) metrics into Supabase for a date range.

Usage:
    cd /root/morning_brief_v2
    .venv/bin/python backfill_helio.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv("/root/morning_brief_v2/.env")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import HUAMI_TOKEN, HELIO_HOST
from db.client import get_client, upsert_brief, upsert_helio_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VPS_URL = f"http://{HELIO_HOST}/helio"
ZEPP_CONFIG = "/root/zepp-health-cli/config.json"

START_DATE = date(2026, 6, 10)
END_DATE   = date.today()

ZEPP_USER_ID = "1196956530"
ZEPP_HOST = "api-mifit-us3.zepp.com"


def _ms(dt: date) -> int:
    """Convert date to UTC milliseconds since epoch."""
    return int(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp() * 1000)


def _refresh_app_token() -> str | None:
    """Re-run huami-token to get fresh app_token, update config.json and .env."""
    logger.info("Refreshing app_token from huami-token...")
    try:
        email = os.environ.get("ZEPP_LOGIN", os.environ.get("ZEPP_EMAIL", "rdshuvalov@gmail.com"))
        password = os.environ.get("ZEPP_PASSWORD", "")
        r = subprocess.run(
            ["huami-token", "-m", "amazfit",
             "-e", email,
             "-p", password,
             "-n"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            logger.error("huami-token failed: %s", r.stderr[-500:])
            return None

        app_token = None
        for line in r.stdout.splitlines():
            if line.startswith("app_token="):
                app_token = line.split("=", 1)[1].strip()
                break
        if not app_token:
            for line in r.stderr.splitlines():
                if "App token:" in line:
                    app_token = line.split("App token:", 1)[1].strip()
                    break
        if not app_token:
            logger.error("app_token not found in huami-token output")
            return None

        # Update config.json
        cfg = json.load(open(ZEPP_CONFIG))
        cfg["app_token"] = app_token
        json.dump(cfg, open(ZEPP_CONFIG, "w"))
        logger.info("app_token updated in config.json")

        # Update .env HUAMI_TOKEN
        env_path = "/root/morning_brief_v2/.env"
        env_lines = open(env_path).readlines()
        for i, line in enumerate(env_lines):
            if line.startswith("HUAMI_TOKEN="):
                env_lines[i] = f"HUAMI_TOKEN={app_token}\n"
                break
        open(env_path, "w").writelines(env_lines)
        logger.info("HUAMI_TOKEN updated in .env")

        os.environ["ZEPP_APP_TOKEN"] = app_token
        return app_token
    except Exception as e:
        logger.error("Token refresh failed: %s", e)
        return None


def _get_token() -> str:
    try:
        cfg = json.load(open(ZEPP_CONFIG))
        return cfg.get("app_token", "")
    except Exception:
        return os.environ.get("ZEPP_APP_TOKEN", "")


def _fetch_v2(event_type: str, sub_type: str, from_ms: int, to_ms: int) -> dict | None:
    """GET /v2/users/me/events."""
    import requests
    app_token = _get_token()
    if not app_token:
        app_token = _refresh_app_token()
        if not app_token:
            return None

    url = f"https://{ZEPP_HOST}/v2/users/me/events"
    params = {
        "eventType": event_type,
        "subType": sub_type,
        "from": from_ms,
        "to": to_ms,
        "limit": 10,
        "reverse": 1,
    }
    headers = {"apptoken": app_token, "Content-Type": "application/json"}

    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 401:
                if attempt == 0:
                    new_token = _refresh_app_token()
                    if new_token:
                        headers["apptoken"] = new_token
                    continue
                logger.error("401 for %s/%s", event_type, sub_type)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("error %s/%s: %s", event_type, sub_type, e)
            return None
    return None


def _fetch_date_string(event_type: str, sub_type: str, from_iso: str, to_iso: str) -> dict | None:
    """GET /users/{uid}/events/dateString."""
    import requests
    app_token = _get_token()
    if not app_token:
        return None

    uid = ZEPP_USER_ID
    url = f"https://{ZEPP_HOST}/users/{uid}/events/dateString"
    params = {
        "eventType": event_type,
        "subType": sub_type,
        "from": from_iso,
        "to": to_iso,
        "timeZone": "UTC",
        "limit": 5,
        "reverse": 0,
        "userId": uid,
    }
    headers = {"apptoken": app_token, "Content-Type": "application/json"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("dateString error %s/%s: %s", event_type, sub_type, e)
        return None


def _fetch_band_data(d: date) -> dict | None:
    """Fetch /v1/data/band_data.json for a specific date → raw sleep JSON or None."""
    import requests
    app_token = _get_token()
    if not app_token:
        app_token = _refresh_app_token()
        if not app_token:
            return None

    url = f"https://{ZEPP_HOST}/v1/data/band_data.json"
    params = {
        "userid": ZEPP_USER_ID,
        "from_date": d.isoformat(),
        "to_date": d.isoformat(),
        "query_type": "detail",
        "byteLength": 8,
        "device_type": 0,
    }
    headers = {"apptoken": app_token, "Content-Type": "application/json"}

    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code == 401:
                if attempt == 0:
                    new_token = _refresh_app_token()
                    if new_token:
                        headers["apptoken"] = new_token
                    continue
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("band_data error %s: %s", d, e)
            return None
    return None


def _decode_stress_samples(stress_info_b64: str) -> list[int]:
    """Decode base64 stress_info binary → list of 0-100 stress values (uint8)."""
    try:
        raw = base64.b64decode(stress_info_b64)
        # Each byte = uint8 stress value (0-100 range, 254=sentinel/missing)
        values = struct.unpack(f"<{len(raw)}B", raw)
        # Filter out sentinel values
        return [v for v in values if v < 254]
    except Exception:
        return []


def _parse_readiness(data: dict) -> dict:
    """Parse readiness/watch_score → {readiness, physical, mental, hrv_score, sleep_hrv, rhr}."""
    items = data.get("items", [])
    if not items:
        return {}
    v = items[0].get("value", {})
    return {
        "readiness":  v.get("rdnsScore"),
        "physical":   v.get("phyScore"),
        "mental":     v.get("mentScore"),
        "hrv_score":  v.get("hrvScore"),
        "sleep_hrv":  v.get("sleepHRV"),
        "rhr":        v.get("sleepRHR"),
    }


def _parse_daily_health(data: dict) -> dict:
    """Parse DailyHealth/summary → {steps, kcal}."""
    items = data.get("items", [])
    if not items:
        return {}
    samples = (items[0].get("value") or {}).get("samples", [])
    if not samples:
        return {}
    s = samples[0]
    return {
        "steps": s.get("totalSteps"),
        "kcal":  s.get("totalCalories"),
    }


def _parse_body_battery(data: dict) -> dict:
    """Parse Charge/real_data → hybrid_energy (body battery).
    
    Samples are per-minute. Returns last total value as hybrid_energy.
    """
    items = data.get("items", [])
    if not items:
        return {}
    samples = (items[0].get("value") or {}).get("samples", [])
    if not samples:
        return {}
    if "total" not in samples[-1]:
        return {}
    return {"hybrid_energy": int(round(samples[-1].get("total", 0)))}


def _parse_stress(data: dict) -> dict:
    """Parse Charge/stress_data → {stress}.

    Samples contain base64-encoded stressInfo with per-minute stress values (0-100).
    Returns the average stress value for the day.
    """
    items = data.get("items", [])
    if not items:
        return {}
    samples = (items[0].get("value") or {}).get("samples", [])
    if not samples:
        return {}
    all_vals = []
    for s in samples:
        info = s.get("stressInfo", "")
        if info:
            all_vals.extend(_decode_stress_samples(info))
    if not all_vals:
        return {}
    return {"stress": int(round(sum(all_vals) / len(all_vals)))}


def _parse_hrv(data: dict) -> dict:
    """Parse hrv_sdnn/real_data → {hrv_score} (SDNN in ms)."""
    items = data.get("items", [])
    if not items:
        return {}
    samples = (items[0].get("value") or {}).get("samples", [])
    if not samples:
        return {}
    sdnn_vals = [s["sdnn"] for s in samples if "sdnn" in s]
    if not sdnn_vals:
        return {}
    return {"hrv_score": int(round(sum(sdnn_vals) / len(sdnn_vals)))}


def _parse_spo2(data: dict) -> dict:
    """Parse blood_oxygen/odi → {spo2} (average SpO2 score)."""
    items = data.get("items", [])
    if not items:
        return {}
    score_vals = [int(item.get("score", 0)) for item in items
                  if item.get("score", "0") not in ("", "-1")]
    if not score_vals:
        return {}
    return {"spo2": round(sum(score_vals) / len(score_vals), 1)}


def _parse_sleep_from_band(band_data: dict) -> dict:
    """Parse band_data summary → {sleep_duration_min, sleep_score, deep_sleep_pct}.

    slp fields:
      - dp: deep sleep minutes
      - lt: light sleep minutes
      - ss: sleep score (0-100)
      - stage[]: segments with start/stop/minute offsets and mode
                mode: 4=light, 5=deep, 8=REM, 7=awake
    """
    data = band_data.get("data", [])
    if not data:
        return {}

    summary_b64 = data[0].get("summary", "")
    if not summary_b64:
        return {}

    try:
        summary = json.loads(base64.b64decode(summary_b64).decode("utf-8"))
    except Exception:
        return {}

    slp = summary.get("slp", {})
    dp = slp.get("dp", 0)   # deep minutes
    lt = slp.get("lt", 0)   # light minutes
    ss = slp.get("ss", 0)   # sleep score

    total = dp + lt
    deep_pct = round(dp / total * 100, 1) if total > 0 else 0.0

    return {
        "sleep_duration_min": total,
        "sleep_score": ss if ss > 0 else None,
        "deep_sleep_pct": deep_pct,
    }


def get_or_create_brief(date_str: str) -> str | None:
    sb = get_client()
    existing = sb.table("briefs").select("id").eq("date", date_str).maybe_single().execute()
    data = existing.data if hasattr(existing, "data") else existing
    if data:
        return data["id"]

    result = upsert_brief(date_str)
    if isinstance(result, list) and result:
        return result[0].get("id") if isinstance(result[0], dict) else None
    if isinstance(result, dict):
        return result.get("id")
    return None


def backfill_date(d: date) -> bool:
    date_str = d.isoformat()
    today = date.today()

    if d > today:
        logger.info("Skipping future date %s", date_str)
        return False

    logger.info("Processing %s", date_str)

    from_ms = _ms(d)
    to_ms = from_ms + 86400 * 1000 - 1
    from_iso = d.isoformat()
    to_iso = (d + timedelta(days=1)).isoformat()

    # Fetch all metrics
    readiness_raw    = _fetch_v2("readiness",       "watch_score",    from_ms, to_ms)
    time.sleep(0.5)
    daily_raw        = _fetch_v2("DailyHealth",      "summary",        from_ms, to_ms)
    time.sleep(0.5)
    body_battery_raw = _fetch_v2("Charge",           "real_data",      from_ms, to_ms)
    time.sleep(0.5)
    stress_raw       = _fetch_v2("Charge",           "stress_data",    from_ms, to_ms)
    time.sleep(0.5)
    hrv_raw          = _fetch_v2("hrv_sdnn",         "real_data",      from_ms, to_ms)
    time.sleep(0.5)
    spo2_raw         = _fetch_date_string("blood_oxygen", "odi",        from_iso, to_iso)
    time.sleep(0.5)
    band_raw         = _fetch_band_data(d)

    readiness  = _parse_readiness(readiness_raw)    if readiness_raw     else {}
    daily      = _parse_daily_health(daily_raw)      if daily_raw         else {}
    body_batt  = _parse_body_battery(body_battery_raw) if body_battery_raw else {}
    stress     = _parse_stress(stress_raw)           if stress_raw        else {}
    hrv        = _parse_hrv(hrv_raw)                 if hrv_raw           else {}
    spo2       = _parse_spo2(spo2_raw)               if spo2_raw          else {}
    sleep      = _parse_sleep_from_band(band_raw)    if band_raw          else {}

    metrics = {**readiness, **daily, **body_batt, **stress, **hrv, **spo2, **sleep}

    if not metrics:
        logger.warning("No Helio metrics for %s, skipping", date_str)
        return False

    # POST to VPS endpoint
    try:
        import httpx
        payload = {"source": "helio_strap", "date": date_str, **metrics}
        resp = httpx.post(VPS_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Posted to VPS /helio for %s", date_str)
    except Exception as e:
        logger.warning("VPS POST failed for %s: %s", date_str, e)

    # Upsert to Supabase
    brief_id = get_or_create_brief(date_str)
    if not brief_id:
        logger.error("Failed to get or create brief for %s", date_str)
        return False

    upsert_helio_metrics(brief_id, date_str, metrics)
    logger.info("Stored helio_metrics for %s: %s", date_str, metrics)
    return True


def main():
    logger.info("Helio backfill: %s → %s", START_DATE, END_DATE)

    current = START_DATE
    success = 0
    failed = 0

    while current <= END_DATE:
        try:
            ok = backfill_date(current)
            if ok:
                success += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Unhandled error for %s: %s", current, e)
            failed += 1

        current += timedelta(days=1)
        time.sleep(2)

    logger.info("Done. success=%d failed=%d", success, failed)


if __name__ == "__main__":
    main()
