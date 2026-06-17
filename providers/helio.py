"""Helio VPS data provider.

流れ:
1. Fetch readiness/activity data from Zepp/Huami API via zepp-health-cli
2. POST to VPS endpoint http://109.123.251.115:18792/helio

Auth: app_token from HUAMI_TOKEN (Zepp API) + HUAMI_TOKEN as Bearer for VPS.

Auto-refresh: if zepp-health-cli returns 401, re-generate app_token
from huami-token and retry once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import date

import httpx

from config import HELIO_HOST, HUAMI_TOKEN
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)

VPS_URL = f"http://{HELIO_HOST}/helio"

# Path to config.json (zepp-health-cli stores app_token here)
ZEPP_CONFIG = "/root/zepp-health-cli/config.json"
ZEPP_SH = "/root/zepp_morning_fetch.sh"


def _refresh_app_token() -> bool:
    """Re-run huami-token to get fresh app_token, update config.json and .env.
    Returns True on success, False on failure.
    Uses --no-logout to prevent token invalidation after fetch.
    """
    logger.info("Refreshing app_token from huami-token (--no-logout)...")
    try:
        email = os.environ.get("ZEPP_LOGIN", os.environ.get("ZEPP_EMAIL", "rdshuvalov@gmail.com"))
        password = os.environ.get("ZEPP_PASSWORD", "")
        # Run huami-token (must use -m amazfit, -p password, -n for no-logout)
        r = subprocess.run(
            ["huami-token", "-m", "amazfit",
             "-e", email,
             "-p", password,
             "-n"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            logger.error("huami-token failed: %s", r.stderr[-300:])
            return False

        # Extract app_token from DEBUG stderr lines ("App token: <token>")
        app_token = None
        for line in r.stderr.splitlines():
            if "App token:" in line:
                app_token = line.split("App token:", 1)[1].strip()
                break
        if not app_token:
            logger.error("app_token not found in huami-token output")
            return False

        # Update config.json
        cfg = json.load(open(ZEPP_CONFIG))
        cfg["app_token"] = app_token
        json.dump(cfg, open(ZEPP_CONFIG, "w"))
        logger.info("app_token updated in config.json")

        # Update .env (HUAMI_TOKEN line)
        env_path = "/root/morning_brief_v2/.env"
        env_lines = open(env_path).readlines()
        updated = False
        for i, line in enumerate(env_lines):
            if line.startswith("HUAMI_TOKEN="):
                env_lines[i] = f"HUAMI_TOKEN={app_token}\n"
                updated = True
                break
        if updated:
            open(env_path, "w").writelines(env_lines)
            logger.info("HUAMI_TOKEN updated in .env")

        return True

    except Exception as e:
        logger.error("Token refresh failed: %s", e)
        return False


def _fetch_zepp(preset: str, days: int = 1) -> dict | None:
    """Call zepp-health-cli and return parsed JSON, or None on error.
    On 401 — attempt token refresh once, then retry.
    """
    for attempt in range(2):
        try:
            result = subprocess.run(
                [
                    "python3", "zepp_health.py",
                    "events", "--preset", preset,
                    "--days", str(days), "--json",
                ],
                capture_output=True,
                text=True,
                cwd="/root/zepp-health-cli",
                timeout=30,
            )

            if result.returncode == 0:
                return json.loads(result.stdout)

            # Check for 401 in stderr
            stderr_lower = result.stderr.lower()
            if "401" in stderr_lower or "unauthorized" in stderr_lower:
                if attempt == 0:
                    logger.warning("zepp_health.py %s got 401, refreshing token...", preset)
                    if _refresh_app_token():
                        continue  # retry
                logger.error("zepp_health.py %s still failing after token refresh", preset)
            else:
                logger.warning("zepp_health.py %s failed: %s", preset, result.stderr[-200:])

            return None

        except Exception as e:
            logger.warning("zepp_health.py %s error: %s", preset, e)
            return None

    return None


def _parse_readiness(data: dict) -> dict:
    """Parse readiness preset into {readiness, physical, mental, hrv_score, sleep_hrv, rhr}."""
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
    """Parse daily-health preset into {steps, kcal, active_minutes, distance}."""
    items = data.get("items", [])
    if not items:
        return {}
    samples = (items[0].get("value") or {}).get("samples", [])
    if not samples:
        return {}
    s = samples[0]
    return {
        "steps":          s.get("totalSteps"),
        "kcal":           s.get("totalCalories"),
        "active_minutes": s.get("activityBytes"),
        "distance":       s.get("totalDistance"),
    }


class HelioProvider(DataProvider):
    name = "helio"

    async def fetch(self) -> ProviderResult:
        try:
            today = date.today().isoformat()

            # Fetch readiness and daily-health concurrently
            readiness_task = asyncio.to_thread(_fetch_zepp, "readiness", 1)
            daily_task     = asyncio.to_thread(_fetch_zepp, "daily-health", 1)

            readiness_raw, daily_raw = await asyncio.gather(readiness_task, daily_task)

            readiness = _parse_readiness(readiness_raw) if readiness_raw else {}
            daily     = _parse_daily_health(daily_raw) if daily_raw else {}

            metrics = {**readiness, **daily}
            if not metrics:
                return self._fail("No Helio data received")

            # POST to VPS
            payload = {"source": "helio_strap", "date": today, **metrics}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(VPS_URL, json=payload)
                resp.raise_for_status()

            return self._ok(metrics)

        except Exception as e:
            logger.warning("Helio fetch error: %s", e)
            return self._fail(str(e))
