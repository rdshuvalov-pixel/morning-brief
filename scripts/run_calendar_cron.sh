#!/bin/bash
# /root/morning_brief_v2/scripts/run_calendar_cron.sh
# Daily Google Calendar fetch for TODAY. Invoked by run_provider.sh from
# /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.

set -eo pipefail

cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/calendar-$DATE_UTC.log"

echo "[calendar] start $(date -u +%FT%TZ)" >> "$LOG"

./.venv/bin/python - <<'PY' >> "$LOG" 2>&1
import asyncio
from providers.calendar import CalendarProvider
r = asyncio.run(CalendarProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error} events={len((r.data or {}).get('events', []))}")
PY
RC=$?

echo "[calendar] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC