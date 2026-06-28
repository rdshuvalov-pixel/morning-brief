"""Supabase client wrapper for morning_brief_v2."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Generator

from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

_supabase_client: Client | None = None


def get_client() -> Client:
    global _supabase_client
    if _supabase_client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        options = SyncClientOptions(schema="morning_brief_v2")
        _supabase_client = create_client(url, key, options=options)
    return _supabase_client


@contextmanager
def client() -> Generator[Client, None, None]:
    yield get_client()


def upsert_brief(date_val: str) -> dict[str, Any]:
    sb = get_client()
    result = sb.table("briefs").upsert(
        {"date": str(date_val), "collected_at": datetime.utcnow().isoformat()},
        on_conflict="date",
    ).execute()
    data = result.data if hasattr(result, 'data') else result
    return data[0] if data else {}


# garmin_metrics column types (matches db/migrations/001_initial_schema.sql + 002)
# Coerce provider output (often floats from Garmin API) to match the schema.
_GARMIN_INT_COLS = {
    "sleep_duration_min", "sleep_score", "hrv", "body_battery", "rhr",
    "training_readiness", "stress", "total_steps",
    "resting_kcal", "active_kcal",
}
_GARMIN_NUMERIC_COLS = {
    "deep_sleep_pct", "spo2", "skin_temp", "distance_km",
}


def _coerce_garmin_row(metrics: dict[str, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for k, v in metrics.items():
        if v is None:
            coerced[k] = None
            continue
        if k in _GARMIN_INT_COLS:
            coerced[k] = int(round(float(v)))
        elif k in _GARMIN_NUMERIC_COLS:
            coerced[k] = round(float(v), 2)
        else:
            coerced[k] = v
    return coerced


def upsert_garmin_metrics(brief_id: str, date_val: str, metrics: dict[str, Any]) -> dict[str, Any]:
    sb = get_client()
    row = {"brief_id": brief_id, "date": str(date_val), **_coerce_garmin_row(metrics)}
    result = sb.table("garmin_metrics").upsert(row, on_conflict="date").execute()
    data = result.data if hasattr(result, 'data') else result
    return data[0] if data else {}


def upsert_helio_metrics(brief_id: str, date_val: str, metrics: dict[str, Any]) -> dict[str, Any]:
    sb = get_client()
    row = {"brief_id": brief_id, "date": str(date_val), **metrics}
    result = sb.table("helio_metrics").upsert(row, on_conflict="date").execute()
    data = result.data if hasattr(result, 'data') else result
    return data[0] if data else {}


def upsert_food_log(brief_id: str, date_val: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("food_log").delete().eq("brief_id", brief_id).execute()
    if not entries:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in entries]
    result = sb.table("food_log").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_weather_log(brief_id: str, date_val: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("weather_log").delete().eq("brief_id", brief_id).execute()
    if not entries:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in entries]
    result = sb.table("weather_log").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_calendar_events(brief_id: str, date_val: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("calendar_events").delete().eq("brief_id", brief_id).execute()
    if not events:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in events]
    result = sb.table("calendar_events").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_tasks(brief_id: str, date_val: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("tasks").delete().eq("brief_id", brief_id).execute()
    if not tasks:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **t} for t in tasks]
    result = sb.table("tasks").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def get_brief(date_val: date) -> dict[str, Any] | None:
    sb = get_client()
    result = sb.table("briefs").select("*").eq("date", str(date_val)).maybe_single().execute()
    return result.data if hasattr(result, 'data') else result


def get_garmin_metrics(date_val: date) -> dict[str, Any] | None:
    sb = get_client()
    result = sb.table("garmin_metrics").select("*").eq("date", str(date_val)).maybe_single().execute()
    return result.data if hasattr(result, 'data') else result


def get_helio_metrics(date_val: date) -> dict[str, Any] | None:
    sb = get_client()
    result = sb.table("helio_metrics").select("*").eq("date", str(date_val)).maybe_single().execute()
    return result.data if hasattr(result, 'data') else result


def get_food_log(date_val: date) -> list[dict[str, Any]]:
    sb = get_client()
    result = sb.table("food_log").select("*").eq("date", str(date_val)).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_weather_log(date_val: date) -> list[dict[str, Any]]:
    sb = get_client()
    result = sb.table("weather_log").select("*").eq("date", str(date_val)).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_calendar_events(date_val: date) -> list[dict[str, Any]]:
    sb = get_client()
    result = sb.table("calendar_events").select("*").eq("date", str(date_val)).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_tasks(date_val: date) -> list[dict[str, Any]]:
    sb = get_client()
    result = sb.table("tasks").select("*").eq("date", str(date_val)).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []
