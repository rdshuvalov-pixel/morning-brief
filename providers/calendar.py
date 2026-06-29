"""Google Calendar provider via gws CLI.

CLI is `gws-cli` (not `gws` - the latter is the python package name).
Resolves the binary via:
  1. $PATH lookup
  2. /usr/local/lib/hermes-agent/venv/bin/gws-cli (where Hermes ships it)
  3. anything found by `shutil.which("gws-cli")`

Subcommand is `calendar list` with `--from`/`--to` (ISO 8601), NOT `--date`.
If the CLI is not authenticated / not configured, we return an empty event
list instead of failing - the brief is still useful without calendar data,
and a hard fail prevents food/weather/etc. from being written.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import date

from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)

_GWS_CLI_CANDIDATES = (
    "gws-cli",                                       # 1) PATH
    "/usr/local/lib/hermes-agent/venv/bin/gws-cli",  # 2) Hermes-shipped location
)


def _resolve_gws_cli() -> str | None:
    for cand in _GWS_CLI_CANDIDATES:
        if os.path.isabs(cand) and os.path.exists(cand):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


class CalendarProvider(DataProvider):
    name = "calendar"

    async def fetch(self) -> ProviderResult:
        cli = _resolve_gws_cli()
        if not cli:
            logger.warning("gws-cli not found in PATH or /usr/local/lib/hermes-agent/venv/bin")
            return self._ok({"events": []})

        target = date.today()
        # gws-cli requires ISO 8601 with timezone offset or Z suffix;
        # plain "YYYY-MM-DDTHH:MM:SS" returns Bad Request.
        from_iso = f"{target.isoformat()}T00:00:00Z"
        to_iso   = f"{target.isoformat()}T23:59:59Z"

        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "calendar", "list",
                "--from", from_iso, "--to", to_iso, "--max", "30",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as e:
            logger.warning("gws-cli exec failed: %s", e)
            return self._ok({"events": []})

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            # OAuth-not-configured, credentials missing, etc. - dont block the brief.
            logger.warning("gws-cli calendar list failed (rc=%s): %s", proc.returncode, err[:200])
            return self._ok({"events": []})

        # gws-cli may emit Rich table OR structured JSON.
        # Try JSON first (modern format with security markers); fall back to line parser.
        events: list[dict] = []
        raw = stdout.decode(errors="replace")
        try:
            doc = json.loads(raw)
            ev_list: list[dict] = []
            if isinstance(doc, dict) and doc.get("status") == "success":
                # New format: {"events": {"data": "<JSON string of events>", ...}}
                inner = doc.get("events") or {}
                if isinstance(inner, dict):
                    data_str = inner.get("data")
                    if isinstance(data_str, str):
                        ev_list = json.loads(data_str)
                    elif isinstance(inner.get("items"), list):
                        ev_list = inner["items"]
                elif isinstance(inner, list):
                    ev_list = inner
            elif isinstance(doc, list):
                ev_list = doc
            for ev in ev_list:
                title = (ev.get("summary") or ev.get("title") or "").strip()
                if not title:
                    continue
                start = ev.get("start")
                # start may be {"dateTime": "...", "timeZone": "..."} or {"date": "..."} for all-day,
                # or a plain ISO string in some API responses.
                if isinstance(start, dict):
                    start_dt = start.get("dateTime") or start.get("date") or ""
                else:
                    start_dt = start or ev.get("start_time") or ""
                # extract HH:MM from ISO 8601
                m = re.search(r"(\d{2}:\d{2})", start_dt)
                start_hhmm = m.group(1) if m else start_dt
                end = ev.get("end")
                if isinstance(end, dict):
                    end_dt_str = end.get("dateTime") or end.get("date") or ""
                else:
                    end_dt_str = end or ""
                duration_min = None
                if start_dt and end_dt_str:
                    try:
                        # Strip timezone for simple parsing
                        s = re.sub(r"[Z+\-]\d{2}:?\d{2}$", "", start_dt).replace("T", " ")
                        e = re.sub(r"[Z+\-]\d{2}:?\d{2}$", "", end_dt_str).replace("T", " ")
                        from datetime import datetime as _dt
                        ds = _dt.fromisoformat(s)
                        de = _dt.fromisoformat(e)
                        duration_min = int((de - ds).total_seconds() // 60)
                    except Exception:
                        pass
                events.append({
                    "title":            title,
                    "start_time":       start_hhmm,
                    "duration_minutes": duration_min,
                    "location":         ev.get("location") or None,
                })
            if events:
                return self._ok({"events": events})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        # Fallback: line-based parser for legacy Rich-table output.
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"(\d{1,2}:\d{2})\s+(.+)", line)
            if m:
                events.append({
                    "title":            m.group(2).strip(),
                    "start_time":       m.group(1),
                    "duration_minutes": None,
                })
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})(?::\d{2})?\s+(.+)", line)
            if m:
                events.append({
                    "title":            m.group(2).strip(),
                    "start_time":       m.group(1).split("T")[-1][:5],
                    "duration_minutes": None,
                })

        return self._ok({"events": events})