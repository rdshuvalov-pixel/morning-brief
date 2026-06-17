"""OpenWeather provider using /data/2.5/forecast (free tier).

GET https://api.openweathermap.org/data/2.5/forecast?q=City&appid=KEY&units=metric&lang=ru
Returns 5-day forecast every 3 hours.
Groups into morning (09:00), day (15:00), evening (21:00) periods.
"""

from __future__ import annotations

import logging

import httpx

from config import OPENWEATHER_API_KEY, WEATHER_LAT, WEATHER_LON
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)


class WeatherProvider(DataProvider):
    name = "weather"

    async def fetch(self) -> ProviderResult:
        try:
            url = "https://api.openweathermap.org/data/2.5/forecast"
            params = {
                "lat": WEATHER_LAT,
                "lon": WEATHER_LON,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
                "lang": "ru",
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            periods = _group_forecast(data.get("list", []))
            return self._ok({"periods": periods})

        except Exception as e:
            logger.warning("Weather fetch error: %s", e)
            return self._fail(str(e))


def _group_forecast(items: list[dict]) -> list[dict]:
    """Pick 09:00 / 15:00 / 21:00 slots from forecast list."""
    targets = {"09:00:00": "morning", "15:00:00": "day", "21:00:00": "evening"}
    buckets: dict[str, dict] = {v: None for v in targets.values()}

    for item in items:
        dt_txt = item.get("dt_txt", "")
        for suffix, label in targets.items():
            if dt_txt.endswith(suffix) and buckets[label] is None:
                buckets[label] = {
                    "period":    label,
                    "temp":      round(item.get("main", {}).get("temp"), 1),
                    "wind":      round(item.get("wind", {}).get("speed"), 1),
                    "condition": item.get("weather", [{}])[0].get("description", "").capitalize(),
                }

    out = []
    for label in ("morning", "day", "evening"):
        if buckets[label] is not None:
            out.append(buckets[label])
        else:
            out.append({"period": label, "temp": None, "condition": None, "wind": None})

    return out
