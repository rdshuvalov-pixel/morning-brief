"""Google Calendar provider via gws CLI.

gws calendar list --date YYYY-MM-DD → title, start_time, duration_minutes
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from datetime import date

from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)


class CalendarProvider(DataProvider):
    name = "calendar"

    async def fetch(self) -> ProviderResult:
        try:
            date_val = date.today().isoformat()
            proc = await asyncio.create_subprocess_exec(
                "gws", "calendar", "list", "--date", date_val,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return self._fail(stderr.decode() or "gws calendar list failed")

            events: list[dict] = []
            for line in stdout.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                # Simple parsing: "09:00  Meeting Title"
                m = re.match(r"(\d{2}:\d{2})\s+(.+)", line)
                if m:
                    events.append({
                        "title":           m.group(2).strip(),
                        "start_time":      m.group(1),
                        "duration_minutes": None,
                    })

            return self._ok({"events": events})

        except FileNotFoundError:
            return self._fail("gws CLI not found")
        except Exception as e:
            logger.warning("Calendar fetch error: %s", e)
            return self._fail(str(e))
