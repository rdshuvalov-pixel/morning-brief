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


def get_active_brief_id(date_val: str | date) -> str | None:
    """Return the brief_id whose collected_at is the latest for date_val.

    Used by readers to disambiguate when multiple brief rows exist for the
    same date (re-renders / partial recoveries). Returns None if no brief
    row exists for that date.
    """
    sb = get_client()
    res = (
        sb.table("briefs")
        .select("id, collected_at")
        .eq("date", str(date_val))
        .order("collected_at", desc=True)
        .limit(1)
        .execute()
    )
    data = res.data if hasattr(res, "data") else res
    if not data:
        return None
    return data[0].get("id")


def get_brief_id_for_food_date(food_date_val: date) -> str | None:
    """Return the brief_id of the latest brief whose collected_at is the
    most recent overall (i.e. the brief we're currently rendering for).

    Used by readers of *yesterday's* tables (food_log, food_date = brief_date - 1)
    where the rows are physically stored under date=food_date but were written
    by the most-recent brief (brief.date = today). Looking up
    get_active_brief_id(food_date) would return the brief_id of a *prior*
    brief whose date was food_date — which has been deleted by
    upsert_food_log's delete-by-date, leaving zero rows.
    """
    sb = get_client()
    res = (
        sb.table("briefs")
        .select("id, date, collected_at")
        .order("collected_at", desc=True)
        .limit(1)
        .execute()
    )
    data = res.data if hasattr(res, "data") else res
    if not data:
        return None
    return data[0].get("id")


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
    # Delete-by-date: one day = one canonical set of rows, regardless of which
    # brief_id wrote them previously. Avoids duplicate rows when re-renders
    # allocate a new brief_id (see providers_review.md — "дубли food_log
    # при повторном brief_id" incident 2026-06-28).
    sb.table("food_log").delete().eq("date", str(date_val)).execute()
    if not entries:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in entries]
    result = sb.table("food_log").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_weather_log(brief_id: str, date_val: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("weather_log").delete().eq("date", str(date_val)).execute()
    if not entries:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in entries]
    result = sb.table("weather_log").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_calendar_events(brief_id: str, date_val: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("calendar_events").delete().eq("date", str(date_val)).execute()
    if not events:
        return []
    rows = [{"brief_id": brief_id, "date": str(date_val), **e} for e in events]
    result = sb.table("calendar_events").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_tasks(brief_id: str, date_val: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("tasks").delete().eq("date", str(date_val)).execute()
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
    """Read rows for the ACTIVE brief_id (latest collected_at overall) for date_val.

    The food_log table is keyed by food_date (= brief_date - 1). Rows are
    physically stored under that food_date, but the active writer is the
    LATEST brief (today's brief). So we look up the latest brief_id overall,
    not briefs WHERE date=food_date — that would return a stale brief_id
    whose rows have been deleted by upsert_food_log's delete-by-date step.

    Filters by brief_id to avoid returning stale rows from prior re-renders
    that may have allocated a different brief_id for the same date. If no
    brief row exists yet, returns [].
    """
    bid = get_brief_id_for_food_date(date_val)
    if not bid:
        return []
    sb = get_client()
    result = sb.table("food_log").select("*").eq("brief_id", bid).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_weather_log(date_val: date) -> list[dict[str, Any]]:
    bid = get_active_brief_id(date_val)
    if not bid:
        return []
    sb = get_client()
    result = sb.table("weather_log").select("*").eq("brief_id", bid).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_calendar_events(date_val: date) -> list[dict[str, Any]]:
    bid = get_active_brief_id(date_val)
    if not bid:
        return []
    sb = get_client()
    result = sb.table("calendar_events").select("*").eq("brief_id", bid).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []


def get_tasks(date_val: date) -> list[dict[str, Any]]:
    bid = get_active_brief_id(date_val)
    if not bid:
        return []
    sb = get_client()
    result = sb.table("tasks").select("*").eq("brief_id", bid).execute()
    data = result.data if hasattr(result, 'data') else result
    return data or []
