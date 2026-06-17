"""Aggregator — builds BriefContext from all provider data."""

from __future__ import annotations

from datetime import date

from models import (
    BriefContext, CalendarEvent, GarminData, HelioData,
    FoodEntry, TaskEntry, WeatherEntry,
)
from db.client import (
    get_garmin_metrics, get_helio_metrics,
    get_food_log, get_weather_log,
    get_calendar_events, get_tasks,
)


def collect(brief_id: str) -> BriefContext:
    date_val = date.today()

    g = get_garmin_metrics(date_val)
    h = get_helio_metrics(date_val)
    food_rows     = get_food_log(date_val)
    weather_rows  = get_weather_log(date_val)
    cal_rows      = get_calendar_events(date_val)
    task_rows     = get_tasks(date_val)

    GARMIN_FIELDS = {'sleep_duration_min', 'sleep_score', 'deep_sleep_pct', 'hrv', 'body_battery', 'rhr', 'spo2', 'training_readiness', 'stress', 'skin_temp'}
    HELIO_FIELDS  = {'readiness', 'physical', 'mental', 'hrv_score', 'sleep_hrv', 'rhr', 'steps', 'kcal', 'distance', 'sleep_duration_min', 'sleep_score', 'deep_sleep_pct', 'spo2', 'stress', 'hybrid_energy'}

    garmin = GarminData(**{k: v for k, v in g.items() if k in GARMIN_FIELDS}) if g else None
    helio  = HelioData(**{k: v for k, v in h.items() if k in HELIO_FIELDS}) if h else None

    food     = [FoodEntry(**r) for r in food_rows]
    weather  = [WeatherEntry(**r) for r in weather_rows]
    calendar = [CalendarEvent(**r) for r in cal_rows]
    tasks    = [TaskEntry(title=t["title"], priority=t["priority"]) for t in task_rows]

    return BriefContext(
        brief_id=brief_id,
        date=date_val,
        garmin=garmin,
        helio=helio,
        food=food,
        weather=weather,
        calendar=calendar,
        tasks=tasks,
    )
