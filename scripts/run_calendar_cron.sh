#!/bin/bash
# /root/morning_brief_v2/scripts/run_calendar_cron.sh
# Daily Calendar fetch + DB upsert for TODAY. Invoked by run_provider.sh
# from /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# 2026-07-05: bug A fix — previously fetch-only, never wrote to Supabase.
# Calendar OAuth refresh was also the root cause of the 180s timeout in
# run_provider.sh — Google token refresh can hang. Wrapper still has the
# 180s deadline via run_provider.sh; on timeout the leaf exits 124 and
# run_provider.sh logs it.

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

RESULT=$(./.venv/bin/python - <<'PY' 2>>"$LOG"
import asyncio, json
from providers.calendar import CalendarProvider
r = asyncio.run(CalendarProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error} events={len((r.data or {}).get('events', []))}")
print("WRITE_JSON_BEGIN")
print(json.dumps({"name": "calendar", "status": r.status,
                  "data": r.data, "error": r.error}))
PY
)
echo "$RESULT" >> "$LOG"

PAYLOAD=$(echo "$RESULT" | sed -n '/^WRITE_JSON_BEGIN$/{n;p;}')
if [ -z "$PAYLOAD" ]; then
    echo "[calendar] writer: no JSON payload fetched — DB write skipped" >> "$LOG"
    echo "[calendar] end rc=1 $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$PAYLOAD" >> "$LOG" 2>&1
WRITE_RC=$?
echo "[calendar] writer rc=$WRITE_RC" >> "$LOG"

echo "[calendar] end rc=$WRITE_RC $(date -u +%FT%TZ)" >> "$LOG"
exit $WRITE_RC