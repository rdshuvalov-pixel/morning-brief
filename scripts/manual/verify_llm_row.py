#!/usr/bin/env python3
"""Fail unless one briefs row has the complete LLM narrative contract."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/root/morning_brief_v2")
sys.path.insert(0, str(PROJECT_ROOT))

for _line in (PROJECT_ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key, _value)

from db.client import get_client  # noqa: E402

BLOCKS = ("weather", "tasks", "movement", "calendar", "battery")
REQUIRED_NARRATIVE_FIELDS = ("headline", "lead", "footer_title", "footer_text")


def validate_row(row: dict) -> list[str]:
    missing: list[str] = []
    narrative = row.get("narrative")
    try:
        payload = json.loads(narrative) if isinstance(narrative, str) else narrative
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        missing.append("narrative")
    else:
        for field in REQUIRED_NARRATIVE_FIELDS:
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                missing.append(f"narrative.{field}")

    if not isinstance(row.get("telegram_text"), str) or not row["telegram_text"].strip():
        missing.append("telegram_text")

    meta = row.get("narrative_blocks_meta")
    if not isinstance(meta, dict):
        meta = {}
    for block in BLOCKS:
        value = row.get(f"narrative_{block}")
        if not isinstance(value, str) or not value.strip():
            missing.append(f"narrative_{block}")
        block_meta = meta.get(block)
        if not isinstance(block_meta, dict) or block_meta.get("source") != "llm":
            missing.append(f"narrative_{block}.source")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    datetime.strptime(args.date, "%Y-%m-%d")

    row_result = get_client().table("briefs").select(
        "narrative,telegram_text,narrative_weather,narrative_tasks,"
        "narrative_movement,narrative_calendar,narrative_battery,narrative_blocks_meta"
    ).eq("date", args.date).order("collected_at", desc=True).limit(1).execute()
    rows = row_result.data or []
    if not rows:
        print(f"INCOMPLETE {args.date}: brief row missing", file=sys.stderr)
        return 2

    row = rows[0]
    if not isinstance(row, dict):
        print(f"INCOMPLETE {args.date}: invalid brief row", file=sys.stderr)
        return 2
    missing = validate_row(row)
    if missing:
        print(f"INCOMPLETE {args.date}: {', '.join(missing)}", file=sys.stderr)
        return 2
    print(f"OK {args.date}: narrative + telegram_text + 5 LLM block columns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
