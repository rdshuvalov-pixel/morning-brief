"""Main entrypoint — morning_brief_v2 pipeline.

Usage: python morning_brief_v2/run.py
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from models import ProviderResult
from aggregator import collect
from config import (
    BRIEF_MAX_ATTEMPTS,
    FOOD_LOG_PATH,
    GARMIN_EMAIL,
    GARMIN_PASSWORD,
    HELIO_HOST,
    HUAMI_TOKEN,
    OPENWEATHER_API_KEY,
    SUPABASE_KEY,
    SUPABASE_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TODOIST_API_TOKEN,
    VERCEL_ORG_ID,
    VERCEL_PROJECT_ID,
    VERCEL_TOKEN,
    WEATHER_LAT,
    WEATHER_LON,
)
from narrator import generate
from publisher import deploy, send_telegram
from renderer import render_html, render_telegram
from scorer import Scorer
from db.client import (
    get_client,
    upsert_brief,
    upsert_food_log,
    upsert_garmin_metrics,
    upsert_helio_metrics,
    upsert_tasks,
    upsert_weather_log,
)
from providers.base import DataProvider
from providers.calendar import CalendarProvider
from providers.food import FoodProvider
from providers.garmin import GarminProvider
from providers.helio import HelioProvider
from providers.todoist import TodoistProvider
from providers.weather import WeatherProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def gather_all_providers(brief_id: uuid.UUID, date_val: date) -> list[ProviderResult]:
    providers: list[DataProvider] = [
        GarminProvider(),
        HelioProvider(),
        FoodProvider(),
        WeatherProvider(),
        CalendarProvider(),
        TodoistProvider(),
    ]
    tasks = [p.fetch() for p in providers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[ProviderResult] = []
    for provider, result in zip(providers, results):
        if isinstance(result, Exception):
            out.append(ProviderResult(
                status="unavailable",
                data=None,
                error=str(result),
                source=provider.name,
            ))
        else:
            out.append(result)
            await _persist(result, brief_id, date_val)

    return out


async def _persist(result: ProviderResult, brief_id: uuid.UUID, date_val: date) -> None:
    if result.data is None:
        return
    try:
        if result.source == "garmin":
            upsert_garmin_metrics(str(brief_id), str(date_val), result.data)
        elif result.source == "helio":
            upsert_helio_metrics(str(brief_id), str(date_val), result.data)
        elif result.source == "food":
            entries = result.data.get("entries", [])
            if entries:
                upsert_food_log(str(brief_id), str(date_val), entries)
        elif result.source == "weather":
            periods = result.data.get("periods", [])
            if periods:
                upsert_weather_log(str(brief_id), str(date_val), periods)
        elif result.source == "todoist":
            tasks = result.data.get("tasks", [])
            if tasks:
                upsert_tasks(str(brief_id), str(date_val), tasks)
    except Exception as e:
        logger.warning("Failed to persist %s: %s", result.source, e)


async def main() -> None:
    date_val = date.today()
    sb = get_client()

    # 1. Check duplicate
    existing = sb.table("briefs").select("id").eq("date", date_val.isoformat()).execute()
    if existing.data:
        logger.info("Brief for %s already exists, skipping", date_val)
        return

    # 2. Create brief record
    brief_row = upsert_brief(str(date_val))
    brief_id_str = brief_row.get("id", "")
    if not brief_id_str:
        logger.error("Failed to create brief record")
        return
    brief_id = uuid.UUID(brief_id_str)
    logger.info("Created brief %s for %s", brief_id, date_val)

    # 3. Collect data with retry
    attempt = 1
    results: list[ProviderResult] = []
    while attempt <= BRIEF_MAX_ATTEMPTS:
        results = await gather_all_providers(brief_id, date_val)

        has_sleep = any(r.source == "garmin" and r.data for r in results)
        has_hrv   = any(r.source in ("garmin", "helio") and r.data for r in results)

        if has_sleep and has_hrv:
            logger.info("Got sleep+HRV data on attempt %d", attempt)
            break
        if attempt == BRIEF_MAX_ATTEMPTS:
            logger.warning("Max attempts reached, proceeding anyway")
            break

        logger.info("Attempt %d: missing data (sleep=%s, hrv=%s), retrying in 60s",
                    attempt, has_sleep, has_hrv)
        attempt += 1
        await asyncio.sleep(60)

    # 4. Aggregate
    ctx = collect(str(brief_id))

    # 5. Score
    status = Scorer().score(ctx)

    # 6. LLM narrative
    narrative, narrative_source = generate(ctx, status)
    logger.info("Narrative source: %s", narrative_source)

    # 7. Render
    html = render_html(narrative, ctx, status)
    tg_text = render_telegram(narrative, status, None)

    # 8. Publish (independent)
    brief_url = deploy(html)
    tg_ok = send_telegram(tg_text, brief_url)

    # 9. Update DB (always save narrative, URL/TG may be missing)
    update_cols = {
        "brief_url": brief_url,
        "telegram_text": tg_text,
        "status": status.status,
        "narrative": narrative,
    }
    sb.table("briefs").update(update_cols).eq("id", str(brief_id)).execute()

    logger.info("Done. URL=%s, TG=%s", brief_url, tg_ok)


if __name__ == "__main__":
    asyncio.run(main())
