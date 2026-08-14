"""Backfill garmin_sleep_minutes за 12.08.2026 из лежащих JSON.

Контекст: garmin.py теперь возвращает поминутные ряды. Эта утилита
берёт сырой JSON-ответ Garmin Connect (raw) и раскладывает его в таблицу
garmin_sleep_minutes. Используется для однократного backfill старых дней,
а также как reference parser (если нужно — можно дёрнуть напрямую).

Поля которые заливаем:
  minute_ts — начало минуты (UTC), опорный таймлайн = sleepMovement (591 точка, 1-мин)
  stage     — Garmin activityLevel из sleepLevels (0=awake→3, 1=light→1, 2=deep→2, 3=rem→0); NULL вне окна
  movement  — sleepMovement.activityLevel (float)
  spo2      — wellnessEpochSPO2DataDTOList.spo2Reading (~411 точек, 1-мин)
  hrv       — hrvData.hrvValue (~94 точки, 5-мин; конвертим GMT epoch ms → UTC ISO)
  stress    — sleepStress.value (~157 точек, 2-мин; GMT epoch ms → UTC ISO)
  body_battery — sleepBodyBattery.value (~157 точек, 2-мин; GMT epoch ms → UTC ISO)
  respiration — wellnessEpochRespirationDataDTOList.respirationValue (236 точек; GMT epoch ms → UTC ISO)

Опорный таймлайн: sleepMovement (591 минута). Это покрывает ВСЕ окно сна
(от первого до последнего движения). SpO2/HRV/etc за пределами окна не
заливаем (NULL).

Запуск:
    cd /root/morning_brief_v2
    set -a; source .env; set +a
    python scripts/backfill_garmin_sleep_minutes.py \
        --raw /tmp/garmin-raw-2026-08-12.json \
        --date 2026-08-12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Repo root → morning_brief_v2/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from db.client import upsert_garmin_sleep_minutes  # noqa: E402

# Mapping Garmin sleepLevels activityLevel → наш stage (см. миграция 009)
# Garmin: 0=awake, 1=light, 2=deep, 3=rem
# Наш:   3=awake, 1=light, 2=deep, 0=rem
_GARMIN_LEVEL_TO_STAGE = {0.0: 3, 1.0: 1, 2.0: 2, 3.0: 0}


def _epoch_ms_to_utc_iso(ms: int) -> str:
    """Garmin Connect epoch ms (GMT) → ISO 8601 UTC string."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _epoch_minute_floor(ms: int) -> str:
    """Floor epoch ms до начала минуты → ISO 8601 UTC. Опорный ключ minute_ts."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    dt = dt.replace(second=0, microsecond=0)
    return dt.isoformat()


def _parse_gmt_str(s: str) -> datetime | None:
    """Garmin Connect 'YYYY-MM-DDTHH:MM:SS.s' GMT → aware UTC datetime."""
    if not isinstance(s, str) or not s:
        return None
    try:
        # '2026-08-11T21:00:00.0' или '2026-08-11T22:00:15.0'
        if s.endswith(".0"):
            s = s[:-2]
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _build_stage_map(levels: list[dict]) -> dict[datetime, int]:
    """Build {minute_start_utc: stage} из sleepLevels (окна стадий).

    Каждое окно [startGMT, endGMT) — заполняем каждую минуту стадией.
    """
    out: dict[datetime, int] = {}
    for lvl in levels:
        start = _parse_gmt_str(lvl.get("startGMT", ""))
        end = _parse_gmt_str(lvl.get("endGMT", ""))
        raw_level = lvl.get("activityLevel")
        if start is None or end is None or raw_level is None:
            continue
        stage = _GARMIN_LEVEL_TO_STAGE.get(float(raw_level))
        if stage is None:
            continue
        cur = _floor_to_minute(start)
        end_floor = _floor_to_minute(end)
        # datetime.replace не нормализует minute=60 → используем timedelta.
        while cur < end_floor:
            out[cur] = stage
            cur = cur + timedelta(minutes=1)
    return out


def _build_epoch_map(records: list[dict], value_key: str, ts_key: str = "startGMT") -> dict[datetime, float]:
    """Build {minute_start_utc: value} из records[{ts_key: GMT str, value_key: val}].

    Все timestamps приводим к UTC. Если Garmin вернул локальное время (без Z),
    то в нашем датасете он всегда GMT (смотри реальные данные 12.08) — поэтому
    парсим как naive UTC.
    """
    out: dict[datetime, float] = {}
    for r in records:
        ts_raw = r.get(ts_key)
        val = r.get(value_key)
        if ts_raw is None or val is None:
            continue
        if isinstance(ts_raw, (int, float)):
            dt = datetime.fromtimestamp(ts_raw / 1000.0, tz=timezone.utc)
        else:
            dt = _parse_gmt_str(ts_raw)
        if dt is None:
            continue
        out[_floor_to_minute(dt)] = val
    return out


def _build_hrv_map(hrv_readings: list[dict]) -> dict[datetime, int]:
    """hrvData → {minute_start_utc: hrv_value}.

    Поддерживает ОБА формата, которые возвращает Garmin Connect:
      1. sleep_data.hrvData[i] = {value: float, startGMT: epoch_ms_int}
      2. hrv.hrvReadings[i]    = {hrvValue: int, readingTimeGMT: 'YYYY-MM-DDTHH:MM:SS.s'}
    """
    out: dict[datetime, int] = {}
    for r in hrv_readings:
        val = r.get("value") or r.get("hrvValue")
        ts_raw = (
            r.get("startGMT")           # sleep_data.hrvData: epoch ms
            or r.get("readingTimeGMT")  # hrv.hrvReadings: ISO GMT string
            or r.get("readingTimeLocal")
        )
        if ts_raw is None or val is None:
            continue
        if isinstance(ts_raw, (int, float)):
            dt = datetime.fromtimestamp(ts_raw / 1000.0, tz=timezone.utc)
        else:
            dt = _parse_gmt_str(ts_raw)
        if dt is None:
            continue
        out[_floor_to_minute(dt)] = int(val)
    return out


def build_minute_rows(raw: dict) -> list[dict]:
    """Построить список minute-rows для insert из raw JSON Garmin Connect.

    Опорный таймлайн = sleepMovement (1-мин интервалы, поле activityLevel).
    """
    sd = raw.get("sleep_data", {})

    movement = sd.get("sleepMovement", []) or []
    levels = sd.get("sleepLevels", []) or []
    spo2_recs = sd.get("wellnessEpochSPO2DataDTOList", []) or []
    resp_recs = sd.get("wellnessEpochRespirationDataDTOList", []) or []
    stress_recs = sd.get("sleepStress", []) or []
    bb_recs = sd.get("sleepBodyBattery", []) or []
    hrv_readings = sd.get("hrvData", []) or []

    stage_map = _build_stage_map(levels)
    spo2_map = _build_epoch_map(spo2_recs, "spo2Reading", "epochTimestamp")
    resp_map = _build_epoch_map(resp_recs, "respirationValue", "startTimeGMT")
    stress_map = _build_epoch_map(stress_recs, "value", "startGMT")
    bb_map = _build_epoch_map(bb_recs, "value", "startGMT")
    hrv_map = _build_hrv_map(hrv_readings)

    rows: list[dict] = []
    seen: set[datetime] = set()
    for m in movement:
        start = _parse_gmt_str(m.get("startGMT", ""))
        if start is None:
            continue
        minute = _floor_to_minute(start)
        if minute in seen:
            continue
        seen.add(minute)

        row: dict = {
            "minute_ts": minute.isoformat(),
            "stage": stage_map.get(minute),  # None вне окна стадий
            "movement": m.get("activityLevel"),
            "spo2": spo2_map.get(minute),
            "respiration": resp_map.get(minute),
            "stress": stress_map.get(minute),
            "body_battery": bb_map.get(minute),
            "hrv": hrv_map.get(minute),
        }
        rows.append(row)
    rows.sort(key=lambda r: r["minute_ts"])
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", required=True, help="path to garmin-raw-YYYY-MM-DD.json")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = p.parse_args()

    raw_path = Path(args.raw)
    if not raw_path.exists():
        print(f"ERROR: file not found: {raw_path}", file=sys.stderr)
        return 1

    with open(raw_path) as f:
        raw = json.load(f)

    rows = build_minute_rows(raw)
    print(f"Built {len(rows)} minute-rows from {raw_path}")

    if not rows:
        print("WARNING: no rows to insert (no sleepMovement)", file=sys.stderr)
        return 1

    # Print non-null counts for sanity
    counts = {k: 0 for k in ("stage", "movement", "spo2", "hrv", "stress", "body_battery", "respiration")}
    for r in rows:
        for k in counts:
            if r.get(k) is not None:
                counts[k] += 1
    print("Non-null counts:", counts)

    # Sanity: per-hour aggregate (как pretty JSON)
    from collections import Counter
    per_hour = Counter()
    for r in rows:
        hour = r["minute_ts"][:13]  # YYYY-MM-DDTHH
        per_hour[hour] += 1
    print("Per-hour row count:", dict(sorted(per_hour.items())))

    if "SUPABASE_URL" not in os.environ or "SUPABASE_KEY" not in os.environ:
        print("ERROR: SUPABASE_URL/SUPABASE_KEY not in env", file=sys.stderr)
        return 2

    try:
        inserted = upsert_garmin_sleep_minutes(args.date, rows)
    except Exception as e:
        msg = str(e)
        if "PGRST205" in msg or "does not exist" in msg:
            print("ERROR: table garmin_sleep_minutes does not exist.", file=sys.stderr)
            print("       Apply migration 009_garmin_sleep_minutes.sql first.", file=sys.stderr)
            return 3
        if "42501" in msg or "permission denied" in msg.lower():
            print("ERROR: permission denied.", file=sys.stderr)
            print("       RLS policy not applied — check migration 009.", file=sys.stderr)
            return 4
        raise

    print(f"Inserted/updated {len(inserted)} rows for {args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
