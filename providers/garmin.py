"""Garmin Connect data provider.

Auth: email/password via garminconnect library.
Collects: sleep, HRV, body battery, RHR, SpO2, training readiness, stress.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from garminconnect import Garmin

from config import GARMIN_EMAIL, GARMIN_PASSWORD
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)


# Garmin sleep_levels activityLevel → наша схема stage (см. миграция 009)
# Garmin: 0=awake, 1=light, 2=deep, 3=rem
# Наш:    3=awake, 1=light, 2=deep, 0=rem
_GARMIN_LEVEL_TO_STAGE = {0.0: 3, 1.0: 1, 2.0: 2, 3.0: 0}


def _parse_gmt(s: str) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith(".0"):
            s = s[:-2]
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def build_sleep_minutes_rows(sleep_data: dict, target_date: date) -> list[dict]:
    """Построить список minute-rows для garmin_sleep_minutes из sleep_data.

    Опорный таймлайн — sleepMovement (1-мин интервалы, поле activityLevel).
    Возвращает ISO-форматированные minute_ts (UTC) для каждой минуты окна
    движения; stage/spo2/hrv/stress/body_battery/respiration берутся
    ближайшей предыдущей записью из соответствующего ряда.

    Args:
        sleep_data: ответ garminconnect.Garmin.get_sleep_data(date_str).
        target_date: дата брифа ('YYYY-MM-DD' UTC). Если окно сна пересекает
                     полночь — пишем все минуты под target_date (т.е. ту
                     дату, которую Garmin пометил как calendarDate в DTO).

    Returns: список dict с ключами minute_ts/stage/movement/spo2/hrv/stress/
             body_battery/respiration.
    """
    movement = sleep_data.get("sleepMovement", []) or []
    if not movement:
        return []

    levels = sleep_data.get("sleepLevels", []) or []
    spo2_recs = sleep_data.get("wellnessEpochSPO2DataDTOList", []) or []
    resp_recs = sleep_data.get("wellnessEpochRespirationDataDTOList", []) or []
    stress_recs = sleep_data.get("sleepStress", []) or []
    bb_recs = sleep_data.get("sleepBodyBattery", []) or []
    hrv_readings = sleep_data.get("hrvData", []) or []

    # stage_map: minute_start_utc → stage (0/1/2/3)
    stage_map: dict[datetime, int] = {}
    for lvl in levels:
        start = _parse_gmt(lvl.get("startGMT", ""))
        end = _parse_gmt(lvl.get("endGMT", ""))
        raw = lvl.get("activityLevel")
        if start is None or end is None or raw is None:
            continue
        stage = _GARMIN_LEVEL_TO_STAGE.get(float(raw))
        if stage is None:
            continue
        cur = _floor_minute(start)
        end_floor = _floor_minute(end)
        while cur < end_floor:
            stage_map[cur] = stage
            cur = cur + timedelta(minutes=1)

    # helpers: построить {minute_start_utc: value} из records
    def _epoch_str_map(records, value_key, ts_key="startGMT"):
        out = {}
        for r in records:
            ts = r.get(ts_key)
            v = r.get(value_key)
            if ts is None or v is None:
                continue
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            else:
                dt = _parse_gmt(ts)
            if dt is None:
                continue
            out[_floor_minute(dt)] = v
        return out

    spo2_map = _epoch_str_map(spo2_recs, "spo2Reading", "epochTimestamp")
    resp_map = _epoch_str_map(resp_recs, "respirationValue", "startTimeGMT")
    stress_map = _epoch_str_map(stress_recs, "value", "startGMT")
    bb_map = _epoch_str_map(bb_recs, "value", "startGMT")

    hrv_map: dict[datetime, int] = {}
    for r in hrv_readings:
        val = r.get("value") or r.get("hrvValue")
        ts = (
            r.get("startGMT")
            or r.get("readingTimeGMT")
            or r.get("readingTimeLocal")
        )
        if val is None or ts is None:
            continue
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        else:
            dt = _parse_gmt(ts)
        if dt is None:
            continue
        hrv_map[_floor_minute(dt)] = int(val)

    rows: list[dict] = []
    seen: set[datetime] = set()
    for m in movement:
        start = _parse_gmt(m.get("startGMT", ""))
        if start is None:
            continue
        minute = _floor_minute(start)
        if minute in seen:
            continue
        seen.add(minute)
        rows.append({
            "minute_ts": minute.isoformat(),
            "stage": stage_map.get(minute),
            "movement": m.get("activityLevel"),
            "spo2": spo2_map.get(minute),
            "respiration": resp_map.get(minute),
            "stress": stress_map.get(minute),
            "body_battery": bb_map.get(minute),
            "hrv": hrv_map.get(minute),
        })
    rows.sort(key=lambda r: r["minute_ts"])
    return rows


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

            # Поминутная развертка сна (movement / SpO2 / HRV / stress / BB / respiration)
            # Пишется отдельным batch'ем в garmin_sleep_minutes — см. fetch().
            # Здесь только собираем rows; insert делается снаружи, чтобы не
            # терять агрегаты при ошибке записи time-series.
            sleep_minute_rows: list[dict] = []
            try:
                target_date = datetime.fromisoformat(date_str).date() if isinstance(date_str, str) else date_str
                sleep_minute_rows = build_sleep_minutes_rows(sleep, target_date)
            except Exception as e:
                logger.warning("Garmin sleep minutes build error: %s", e)

            return {
                "sleep_duration_min": sleep_duration_min,
                "sleep_score": sleep_score,
                "deep_sleep_pct": deep_sleep_pct,
                "sleep_minute_rows": sleep_minute_rows,
                "sleep_minute_count": len(sleep_minute_rows),
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
