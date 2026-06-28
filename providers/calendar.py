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
        from_iso = f"{target.isoformat()}T00:00:00"
        to_iso   = f"{target.isoformat()}T23:59:59"

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

        events: list[dict] = []
        # gws-cli outputs a Rich table; extract rows from the data block.
        # Fall back to simple "HH:MM  Title" parsing if no structured rows.
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            # Try HH:MM Title pattern
            m = re.match(r"(\d{1,2}:\d{2})\s+(.+)", line)
            if m:
                events.append({
                    "title":            m.group(2).strip(),
                    "start_time":       m.group(1),
                    "duration_minutes": None,
                })
                continue
            # Try ISO start + title pattern
            m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})(?::\d{2})?\s+(.+)", line)
            if m:
                events.append({
                    "title":            m.group(2).strip(),
                    "start_time":       m.group(1).split("T")[-1][:5],
                    "duration_minutes": None,
                })

        return self._ok({"events": events})