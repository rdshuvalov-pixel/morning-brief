"""Supabase client wrapper for morning_brief_v2."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Generator

from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

logger = logging.getLogger(__name__)

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

# Полный набор колонок garmin_metrics (001 + 002 + 006).
# Провайдер может отдавать поля сверх схемы — например sleep_minute_rows /
# sleep_minute_count предназначены для таблицы garmin_sleep_minutes (миграция
# 009), а не для garmin_metrics. Без whitelist такое поле убивало ВЕСЬ upsert
# с PGRST204, и в БД не попадали даже валидные агрегаты (bb/hrv/sleep/steps).
# См. incident 2026-08-14.
_GARMIN_KNOWN_COLS = _GARMIN_INT_COLS | _GARMIN_NUMERIC_COLS | {
    "brief_id", "date",
}


def _coerce_garmin_row(metrics: dict[str, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in metrics.items():
        if k not in _GARMIN_KNOWN_COLS:
            dropped.append(k)
            continue
        if v is None:
            coerced[k] = None
            continue
        if k in _GARMIN_INT_COLS:
            coerced[k] = int(round(float(v)))
        elif k in _GARMIN_NUMERIC_COLS:
            coerced[k] = round(float(v), 2)
        else:
            coerced[k] = v
    if dropped:
        # Никогда не молчать про отброшенные поля — иначе schema drift
        # прячется до следующего инцидента (health-data-ingestion pitfall).
        logger.warning(
            "garmin_metrics: dropped %d field(s) not in schema: %s",
            len(dropped), sorted(dropped),
        )
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


# garmin_sleep_minutes: time-series поминутной развертки сна.
# См. db/migrations/009_garmin_sleep_minutes.sql.
# Колоночные whitelists защищают от PGRST204 если провайдер отдаст поле
# сверх схемы.
_GARMIN_SLEEP_MINUTES_COLS = (
    "stage", "movement", "spo2", "hrv", "stress",
    "body_battery", "respiration",
)


def upsert_garmin_sleep_minutes(
    date_val: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bulk-upsert поминутных строк сна за date_val.

    Args:
        date_val: 'YYYY-MM-DD' (UTC) — соответствует date в minute_ts::date.
        rows: каждый dict должен содержать 'minute_ts' (ISO timestamptz).
              Остальные поля stage/movement/spo2/hrv/stress/body_battery/
              respiration — nullable, whitelistируются.

    Returns: список вставленных/обновлённых строк.

    Удаляем старые строки за date_val перед insert — одна дата = один
    канонический набор (аналогично upsert_food_log), чтобы при повторном
    fetch со слегка другими данными не плодились дубли.
    """
    sb = get_client()
    sb.table("garmin_sleep_minutes").delete().eq("date", str(date_val)).execute()
    if not rows:
        return []
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        minute_ts = r.get("minute_ts")
        if not minute_ts:
            continue
        row: dict[str, Any] = {"date": str(date_val), "minute_ts": minute_ts}
        for col in _GARMIN_SLEEP_MINUTES_COLS:
            if col in r:
                v = r[col]
                if v is None:
                    row[col] = None
                elif col in ("spo2", "hrv", "stress", "body_battery"):
                    row[col] = int(round(float(v)))
                elif col == "movement":
                    row[col] = round(float(v), 3)
                elif col == "respiration":
                    row[col] = round(float(v), 2)
                else:
                    row[col] = v
        out_rows.append(row)
    if not out_rows:
        return []
    result = sb.table("garmin_sleep_minutes").insert(out_rows).execute()
    return result.data if hasattr(result, "data") else result


def get_garmin_sleep_minutes(date_val: date | str) -> list[dict[str, Any]]:
    """Все поминутные строки сна за date (UTC), отсортированы по minute_ts.

    Используется renderer'ом для построения time-series по SpO2/HRV/BB
    и hypnogram-полоски. Возвращает [] если таблицы нет (PGRST205) —
    не валит весь бриф если миграция ещё не применена.
    """
    sb = get_client()
    try:
        result = (
            sb.table("garmin_sleep_minutes")
            .select("minute_ts,stage,movement,spo2,hrv,stress,body_battery,respiration")
            .eq("date", str(date_val))
            .order("minute_ts")
            .execute()
        )
    except Exception as e:
        msg = str(e)
        if "PGRST205" in msg or "PGRST204" in msg or "does not exist" in msg:
            return []
        raise
    data = result.data if hasattr(result, "data") else result
    return data or []


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


# Column whitelists per list-table — protects against silent zero-insert
# when provider emits a field not in the schema (PGRST204 "column not found").
_CALENDAR_EVENT_COLS = ("title", "start_time", "duration_minutes")
_TASK_COLS = ("title", "priority")


def upsert_calendar_events(brief_id: str, date_val: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("calendar_events").delete().eq("date", str(date_val)).execute()
    if not events:
        return []
    rows = [
        {"brief_id": brief_id, "date": str(date_val),
         **{k: e.get(k) for k in _CALENDAR_EVENT_COLS}}
        for e in events
    ]
    result = sb.table("calendar_events").insert(rows).execute()
    return result.data if hasattr(result, 'data') else result


def upsert_tasks(brief_id: str, date_val: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sb = get_client()
    sb.table("tasks").delete().eq("date", str(date_val)).execute()
    if not tasks:
        return []
    rows = [
        {"brief_id": brief_id, "date": str(date_val),
         **{k: t.get(k) for k in _TASK_COLS}}
        for t in tasks
    ]
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
    data = result.data if hasattr(result, "data") else result
    return data or []


# ── Per-block narrative helpers (5 columns in briefs, миграция 008) ─────────
# Per-block narratives — 5 TEXT-колонок в briefs (narrative_weather/tasks/movement/
# calendar/battery) + 1 JSONB narrative_blocks_meta для отладки.
# Атомарный UPSERT на 1 блок: briefs.UPDATE WHERE id=X (не отдельная таблица).
# History: meta-колонка фиксирует source/chars на момент записи.
from typing import cast


def upsert_narrative_block(
    brief_id: str,
    block_name: str,
    text: str,
    source: str,
    *,
    model: str | None = None,
    chars: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """UPSERT one block narrative into briefs.narrative_{block_name}.

    Args:
        brief_id: briefs.id (UUID str)
        block_name: one of NARRATIVE_BLOCKS = ('weather','tasks','movement','calendar','battery')
        text: 2-4 предложения от LLM (≤500 chars по MAX_BLOCK_CHARS)
        source: 'llm' | 'fallback' | 'empty'

    Updates two columns atomically:
      - briefs.narrative_{block_name} = text
      - briefs.narrative_blocks_meta.{block_name} = {source, model, chars, ts, error}

    Returns: dict of updated row (or {} on failure).
    """
    from datetime import datetime, timezone
    if block_name not in ("weather", "tasks", "movement", "calendar", "battery"):
        raise ValueError(f"invalid block_name: {block_name}")
    sb = get_client()
    text_col = f"narrative_{block_name}"
    ts = datetime.now(timezone.utc).isoformat()
    meta_obj: dict[str, Any] = {
        "source": source,
        "model": model,
        "chars": chars if chars is not None else len(text),
        "ts": ts,
        "error": error,
    }
    # Two writes: (1) set text column, (2) merge meta_obj into narrative_blocks_meta->{block_name}.
    # Supabase/PostgREST не поддерживает jsonb_set в UPDATE через anon-ключ без RPC,
    # поэтому делаем read-modify-write для meta.
    # ВНИМАНИЕ: колонки updated_at в briefs НЕТ (в отличие от старой таблицы).
    # Трогать её мы не будем.
    update_payload: dict[str, Any] = {text_col: text}
    sb.table("briefs").update(update_payload).eq("id", brief_id).execute()

    # Read existing meta, merge block_name entry, write back
    try:
        cur = sb.table("briefs").select("narrative_blocks_meta").eq("id", brief_id).maybe_single().execute()
        cur_data: Any = cur.data  # type: ignore[attr-defined]
        if not isinstance(cur_data, dict):
            cur_data = {}
        cur_meta_obj = cur_data.get("narrative_blocks_meta") if isinstance(cur_data, dict) else None
        cur_meta: dict[str, Any] = cast(dict[str, Any], cur_meta_obj) if isinstance(cur_meta_obj, dict) else {}
        cur_meta[block_name] = meta_obj
        sb.table("briefs").update({"narrative_blocks_meta": cur_meta}).eq("id", brief_id).execute()
    except Exception as e:
        # Meta-update не критичен (text уже записан), не валим весь upsert
        import logging
        logging.getLogger(__name__).warning("narrative_blocks_meta update failed for %s/%s: %s",
                                            brief_id, block_name, str(e)[:200])

    return {"brief_id": brief_id, "block_name": block_name, "text": text, **meta_obj}


def get_block_narratives(date_val: date) -> dict[str, dict[str, Any]]:
    """Read all per-block narratives for the active brief_id of date_val.

    Returns:
        {block_name: {text, source, model, chars, ts, error}}
        — синтетический dict из narrative_* колонок + narrative_blocks_meta.
    """
    bid = get_active_brief_id(date_val)
    if not bid:
        return {}
    sb = get_client()
    cols = ("narrative_weather", "narrative_tasks", "narrative_movement",
            "narrative_calendar", "narrative_battery", "narrative_blocks_meta")
    try:
        result = sb.table("briefs").select(",".join(cols)).eq("id", bid).maybe_single().execute()
    except Exception as e:
        msg = str(e)
        if "PGRST204" in msg or "42703" in msg or "does not exist" in msg:
            return {}
        raise
    row_obj: Any = result.data  # type: ignore[attr-defined]
    if not isinstance(row_obj, dict):
        row_obj = {}
    row: dict[str, Any] = cast(dict[str, Any], row_obj)
    meta_obj = row.get("narrative_blocks_meta")
    meta: dict[str, Any] = cast(dict[str, Any], meta_obj) if isinstance(meta_obj, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for blk in ("weather", "tasks", "movement", "calendar", "battery"):
        text = row.get(f"narrative_{blk}")
        if not isinstance(text, str) or not text:
            continue
        block_meta_obj = meta.get(blk)
        block_meta: dict[str, Any] = cast(dict[str, Any], block_meta_obj) if isinstance(block_meta_obj, dict) else {}
        out[blk] = {
            "text": text,
            "source": block_meta.get("source", "migrated-from-007" if not meta else "unknown"),
            "model": block_meta.get("model"),
            "chars": block_meta.get("chars") or len(text),
            "ts": block_meta.get("ts"),
            "error": block_meta.get("error"),
        }
    return out


def get_block_text(date_val: date, block_name: str) -> str | None:
    """Read just the text for one block. Convenience for render."""
    if block_name not in ("weather", "tasks", "movement", "calendar", "battery"):
        return None
    bid = get_active_brief_id(date_val)
    if not bid:
        return None
    sb = get_client()
    try:
        r = sb.table("briefs").select(f"narrative_{block_name}").eq("id", bid).maybe_single().execute()
    except Exception as e:
        msg = str(e)
        if "PGRST204" in msg or "42703" in msg:
            return None
        raise
    if not r.data or not isinstance(r.data, dict):
        return None
    text_obj: Any = r.data.get(f"narrative_{block_name}")  # type: ignore[attr-defined]
    text = cast(str, text_obj) if isinstance(text_obj, str) else ""
    return text if text else None
