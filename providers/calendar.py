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
import signal
import tempfile
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


async def _run_gws(cli: str, args: list[str], timeout: float = 90.0) -> tuple[int, bytes, bytes]:
    """Run gws-cli with a hard in-provider timeout.

    Critical: stdout/stderr go to *files*, not PIPE. A pipe would let a forked
    grandchild hold the write end open and prevent EOF, deadlocking
    proc.communicate() forever. proc.wait() with a temp-file stdout is immune
    to that class of bug. We also force HOME=/root in the spawn env so the
    OAuth credentials cache is found even when this is spawned from systemd
    (where HOME is not set on the unit).

    Returns:
      (returncode, stdout_bytes, stderr_bytes).
      returncode == -1 means we hit our internal timeout and killed the process
      group; callers MUST treat that as a hard error, not as "no events".
    """
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,                       # own pgid → killpg works
            env={
                # Mirror worker's systemd unit: HOME must be the Hermes-profile
                # home, NOT plain /root — gws-cli's working OAuth config +
                # token.json.enc live at
                # /root/.hermes/profiles/developer/home/.config/gws-cli/, created
                # by an interactive Hermes login. Plain /root has a stale
                # config without tokens, which makes gws-cli try the OAuth
                # redirect and hang without TTY (2026-07-09).
                **os.environ,
                "HOME": os.environ.get("HERMES_HOME", "/root/.hermes/profiles/developer/home"),
                "TERM": "dumb",
                "NO_COLOR": "1",
                "PYTHONUNBUFFERED": "1",
            },
        )
        try:
            # Poll stderr for OAuth-prompt symptom while waiting. If the
            # refresh-token died and gws-cli entered interactive-OAuth mode,
            # we get the 127.0.0.1:8081 URL quickly (within seconds). Kill
            # immediately rather than waiting out the full 90s timeout.
            # 2026-07-09 — see skill pult-calendar-gwscli-hang-pitfall.
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                # Wait a little, but break early if process exits.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(remaining, 2.0))
                    break   # process exited cleanly
                except asyncio.TimeoutError:
                    # peek at stderr — has gws-cli entered OAuth-prompt mode?
                    err_f.seek(0)
                    err_tail = err_f.read().decode(errors="replace")
                    if ("127.0.0.1:8081" in err_tail
                            or "OAuth Authorization Required" in err_tail):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await proc.wait()  # drain
                        out_f.seek(0); err_f.seek(0)
                        return -2, out_f.read() or b"", err_tail.encode()
                    # continue polling until deadline
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Drain files best-effort before returning the timeout sentinel.
            try:
                out_f.seek(0); err_f.seek(0)
                out_b = out_f.read() or b""
                err_b = err_f.read() or b""
            except Exception:
                out_b, err_b = b"", b""
            return -1, out_b, err_b

        out_f.seek(0); err_f.seek(0)
        stdout = out_f.read()
        stderr = err_f.read()
        return proc.returncode, stdout, stderr
    finally:
        # Always close temp files so a stream of timeouts doesn't leak fds.
        try: out_f.close()
        except Exception: pass
        try: err_f.close()
        except Exception: pass


class CalendarProvider(DataProvider):
    name = "calendar"

    async def fetch(self) -> ProviderResult:
        cli = _resolve_gws_cli()
        if not cli:
            # Permanent: gws-cli not installed. Return ok with empty events so
            # the brief still publishes (other providers carry it). pult-button
            # behaviour: same — "I have no events, but I ran." No data loss.
            logger.warning("gws-cli not found in PATH or /usr/local/lib/hermes-agent/venv/bin")
            return self._ok({"events": []})

        target = date.today()
        from_iso = f"{target.isoformat()}T00:00:00Z"
        to_iso   = f"{target.isoformat()}T23:59:59Z"

        try:
            rc, stdout, stderr = await _run_gws(
                cli,
                ["calendar", "list",
                 "--from", from_iso, "--to", to_iso, "--max", "30"],
                timeout=90.0,  # < worker's 120s external timeout → graceful first
            )
        except Exception as e:
            logger.warning("_run_gws crashed: %s", e)
            # CRITICAL: do NOT return _ok({"events":[]}) here — _write_provider.py
            # does DELETE-then-INSERT on ok status, which would silently wipe
            # today's calendar from the DB. Use unavailable + error so the job
            # surfaces as FAILED in the pult UI instead.
            return self._fail(f"calendar: gws-cli spawn crashed: {type(e).__name__}: {e}")

        if rc == -2:
            # gws-cli entered interactive OAuth-prompt (refresh token
            # expired). Killpg already fired; return a descriptive failure.
            err = stderr.decode(errors="replace").strip()
            logger.error("gws-cli OAuth prompt detected — needs re-auth: %s",
                         err[:200])
            return self._fail("calendar: gws-cli needs re-auth (refresh token expired; "
                              "see skill pult-calendar-gwscli-hang-pitfall)")

        if rc == -1:
            # Internal timeout (we killed the process group). Same reasoning:
            # do not silently overwrite the DB with zero rows.
            err = stderr.decode(errors="replace").strip()
            logger.error("gws-cli calendar list timed out after 90s: %s", err[:200])
            return self._fail("calendar: gws-cli timeout 90s")

        if rc != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning("gws-cli calendar list failed (rc=%s): %s", rc, err[:200])
            return self._fail(f"calendar: gws-cli rc={rc}: {err[:200]}")

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