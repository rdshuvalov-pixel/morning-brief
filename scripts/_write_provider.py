#!/usr/bin/env python3
"""Shared writer for cron-driven providers.

Reads the JSON-serialized ProviderResult on stdin (or argv[1]) and writes
the data into the corresponding Supabase table via db.client.upsert_*.

Returns exit 0 on success, 1 if status != ok (no data to write), 2 if DB
write raised an exception.

Wire format (one line of JSON on stdin/argv[1]):
    {"name": "weather", "status": "ok", "data": {"periods": [...]}, "error": null}

Usage from bash:
    RESULT=$(./.venv/bin/python -c "
        import asyncio, json
        from providers.weather import WeatherProvider
        r = asyncio.run(WeatherProvider().fetch())
        print(json.dumps({'name': 'weather', 'status': r.status,
                          'data': r.data, 'error': r.error}))
    ")
    ./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$RESULT"
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# Make package importable when run as a bare script.
sys.path.insert(0, "/root/morning_brief_v2")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("write_provider")


def _read_payload() -> dict | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return json.loads(sys.argv[1])
    # Fallback: stdin
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    return json.loads(raw)


def _target_date() -> date:
    # food_log uses yesterday; everyone else uses today.
    return date.today()


def main() -> int:
    payload = _read_payload()
    if not payload:
        log.error("no payload on argv[1] or stdin")
        return 2
    name = payload.get("name")
    status = payload.get("status")
    data = payload.get("data") or {}
    if status != "ok" or not data:
        log.info("[%s] skip: status=%s (no DB write)", name, status)
        return 1

    # Load env from .env BEFORE supabase reads (mirrors preflight_check.py).
    env_path = Path("/root/morning_brief_v2/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            import os
            os.environ.setdefault(k.strip(), v.strip())

    from db.client import (
        upsert_brief,
        upsert_weather_log,
        upsert_tasks,
        upsert_calendar_events,
        upsert_food_log,
    )

    target = _target_date()
    target_str = target.isoformat()

    # Ensure brief row exists (FK target for list tables).
    brief = upsert_brief(target_str)
    brief_id = (brief or {}).get("id")
    if not brief_id:
        log.error("upsert_brief returned no id")
        return 2

    try:
        if name == "weather":
            periods = data.get("periods", []) if isinstance(data, dict) else data
            upsert_weather_log(brief_id, target_str, periods)
        elif name == "tasks":
            tasks = data.get("tasks", []) if isinstance(data, dict) else data
            upsert_tasks(brief_id, target_str, tasks)
        elif name == "calendar":
            events = data.get("events", []) if isinstance(data, dict) else data
            upsert_calendar_events(brief_id, target_str, events)
        elif name == "food":
            entries = data.get("entries", []) if isinstance(data, dict) else data
            food_date = (target - timedelta(days=1)).isoformat()
            upsert_food_log(brief_id, food_date, entries)
        else:
            log.warning("[%s] no writer registered, skipped", name)
            return 1
    except Exception as e:
        log.exception("[%s] DB write failed: %s", name, e)
        return 2

    log.info("[%s] written to Supabase (brief=%s date=%s)", name, brief_id, target_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())