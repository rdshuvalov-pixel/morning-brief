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

    async def fetch(self, target_date: date | None = None) -> ProviderResult:
        """Fetch Garmin metrics for a given date.

        Args:
            target_date: Day to fetch. If None, defaults to yesterday
                         (preserves the original behaviour for callers
                         that want a closed/settled day).

        Why this matters: Garmin Connect returns same-day data as soon as
        the user wakes up — sleep, HRV, RHR, SpO2, training readiness,
        and the morning Body Battery peak are all available. The previous
        version hard-coded yesterday, which silently dropped every morning
        before the day "closed".
        """
        try:
            client = await self._auth()
            if not client:
                return self._fail("Garmin auth failed")

            if target_date is None:
                target_date = date.today() - timedelta(days=1)
            target_str = target_date.isoformat()

            sleep_data = await self._get_sleep(client, target_str)
            hrv_data = await self._get_hrv_premium(client, target_str)
            daily_data = await self._get_daily_stats(client, target_str)

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

            # Sleep score resolution (Garmin changed this endpoint shape):
            #   1. NEW path (2024+): dailySleepDTO.sleepScores.overall.value
            #   2. OLD path: sleep["sleepScore"]["overall"]  (kept for back-compat)
            # Some days return None while the day is still unclosed — that's
            # expected, not a bug.
            sleep_score = None
            sleep_scores_obj = dto.get("sleepScores") if isinstance(dto, dict) else None
            if isinstance(sleep_scores_obj, dict):
                overall = sleep_scores_obj.get("overall")
                if isinstance(overall, dict) and overall.get("value") is not None:
                    sleep_score = overall["value"]
            if sleep_score is None:
                legacy = sleep.get("sleepScore")
                if isinstance(legacy, dict):
                    sleep_score = legacy.get("overall")

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
            # Body Battery resolution order:
            #   1. peak from `bodyBatteryValuesArray` (always present when the
            #      day has any data; matches what the user sees on the watch)
            #   2. closed-day `max` field (Garmin fills it when the day settles)
            #   3. closed-day `charged` field (drained end-of-day value —
            #      often much lower than what the watch shows in the morning)
            #
            # Rationale: this number feeds the morning brief and tells the
            # user how charged their body is. Peak-from-array matches what
            # they see on the watch face at the moment of reading. Using
            # `charged` here is misleading because Garmin returns it even
            # mid-day as a drain estimate, not a settled value.
            body_battery = None
            if body_battery_data and isinstance(body_battery_data, list) and body_battery_data:
                sample = body_battery_data[0]
                arr = sample.get("bodyBatteryValuesArray") or sample.get("bodyBatteryValues") or []
                levels = [
                    v[1] for v in arr
                    if isinstance(v, (list, tuple)) and len(v) >= 2
                    and isinstance(v[1], (int, float))
                ]
                if levels:
                    body_battery = int(max(levels))
                if body_battery is None:
                    body_battery = sample.get("max")
                if body_battery is None:
                    body_battery = sample.get("charged")

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
