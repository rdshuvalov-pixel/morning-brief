"""Garmin Connect data provider.

Auth: email/password via garminconnect library.
Collects: sleep, HRV, body battery, RHR, SpO2, training readiness, stress.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from garminconnect import Garmin

from config import GARMIN_EMAIL, GARMIN_PASSWORD
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)


class GarminProvider(DataProvider):
    name = "garmin"

    def __init__(self):
        self.email = GARMIN_EMAIL
        self.password = GARMIN_PASSWORD

    async def fetch(self) -> ProviderResult:
        try:
            client = await self._auth()
            if not client:
                return self._fail("Garmin auth failed")

            yesterday = date.today() - timedelta(days=1)
            yesterday_str = yesterday.isoformat()

            sleep_data = await self._get_sleep(client, yesterday_str)
            hrv_data = await self._get_hrv_premium(client, yesterday_str)
            daily_data = await self._get_daily_stats(client, yesterday_str)

            data = {**(sleep_data or {}), **(hrv_data or {}), **(daily_data or {})}
            if not data:
                return self._fail("No Garmin data received")
            return self._ok(data)

        except Exception as e:
            logger.warning("Garmin fetch error: %s", e)
            return self._fail(str(e))

    async def _auth(self) -> Garmin | None:
        try:
            client = Garmin(self.email, self.password)
            # garminconnect.login() is sync but may be slow; run in thread pool
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.login(tokenstore="/root/.garminconnect"),
            )
            return client
        except Exception as e:
            logger.error("Garmin auth error: %s", e)
            return None

    async def _get_sleep(self, client: Garmin, date_str: str) -> dict | None:
        try:
            def _call():
                return client.get_sleep_data(date_str)

            sleep = await asyncio.get_event_loop().run_in_executor(None, _call)
            if not sleep:
                return {}

            dto = sleep.get("dailySleepDTO", {}) if sleep else {}
            deep = dto.get("deepSleepSeconds", 0) or 0
            light = dto.get("lightSleepSeconds", 0) or 0
            rem = dto.get("remSleepSeconds", 0) or 0
            awake = dto.get("awakeSleepSeconds", 0) or 0
            total_sec = deep + light + rem + awake
            sleep_duration_min = total_sec // 60 if total_sec else 0

            sleep_score = sleep.get("sleepScore", {}).get("overall") if isinstance(sleep.get("sleepScore"), dict) else None

            deep_sleep_pct = None
            if total_sec:
                deep_sleep_pct = round(deep / total_sec * 100, 1)

            return {
                "sleep_duration_min": sleep_duration_min,
                "sleep_score": sleep_score,
                "deep_sleep_pct": deep_sleep_pct,
            }
        except Exception as e:
            logger.warning("Garmin sleep fetch error: %s", e)
            return {}

    async def _get_hrv_premium(self, client: Garmin, date_str: str) -> dict | None:
        try:
            def _call():
                return client.get_hrv_data(date_str)

            hrv = await asyncio.get_event_loop().run_in_executor(None, _call)
            if not hrv:
                return {}

            last_night = hrv.get("hrvSummary", {}).get("lastNightAvg") if isinstance(hrv.get("hrvSummary"), dict) else None
            return {"hrv": last_night}
        except Exception as e:
            logger.warning("Garmin HRV fetch error: %s", e)
            return {}

    async def _get_daily_stats(self, client: Garmin, date_str: str) -> dict | None:
        try:
            def _call():
                return client.get_user_summary(date_str)

            daily = await asyncio.get_event_loop().run_in_executor(None, _call)
            if not daily:
                return {}

            d = daily

            # calories
            resting_kcal = d.get("bmrKilocalories")   # покой (BMR)
            active_kcal   = d.get("activeKilocalories")  # активные

            # resting heart rate
            rhr = d.get("restingHeartRate")

            def _call_body_battery():
                return client.get_body_battery(date_str)

            body_battery_data = await asyncio.get_event_loop().run_in_executor(None, _call_body_battery)
            # Use peak charge (Garmin API field `max`) — for the morning brief
            # the end-of-day `charged` value is already drained overnight,
            # so `max` is the meaningful number.
            body_battery = None
            if body_battery_data and isinstance(body_battery_data, list) and len(body_battery_data) > 0:
                sample = body_battery_data[0]
                body_battery = sample.get("max") if sample.get("max") is not None else sample.get("charged")

            # SpO2
            spo2 = d.get("averageSpo2")

            # training readiness via dedicated endpoint
            def _call_training_readiness():
                return client.get_training_readiness(date_str)

            tr_data = await asyncio.get_event_loop().run_in_executor(None, _call_training_readiness)
            training_readiness = None
            if tr_data and isinstance(tr_data, list) and len(tr_data) > 0:
                training_readiness = tr_data[0].get("score")

            # stress
            stress = d.get("averageStressLevel")

            # skin temp (difference from baseline)
            skin_temp = d.get("averageSkinTempDeviation")

            # total steps + distance (Garmin API: distance in meters → km)
            total_steps = d.get("totalSteps")
            distance_m = d.get("totalDistanceMeters")
            distance_km = round(distance_m / 1000, 2) if distance_m else None

            return {
                "rhr": rhr,
                "body_battery": body_battery,
                "spo2": spo2,
                "training_readiness": training_readiness,
                "stress": stress,
                "skin_temp": skin_temp,
                "resting_kcal": resting_kcal,
                "active_kcal": active_kcal,
                "total_steps": total_steps,
                "distance_km": distance_km,
            }
        except Exception as e:
            logger.warning("Garmin daily stats fetch error: %s", e)
            return {}
