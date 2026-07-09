#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_calendar.sh
# Fetch calendar events for today → calendar_events.

set -eo pipefail
cd /root/morning_brief_v2

# Prevent overlapping runs: if another fetch_calendar.sh is still going,
# the new client gets rc=1 immediately instead of stacking on the
# gws-cli geventary that already broke at job 21 (Pitfall 2026-07-09).
LOCK="/tmp/calendar.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[calendar] another instance is running, exiting" >&2
  exit 1
fi

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/calendar-$(date -u +%Y-%m-%dT%H%M).log"

echo "[calendar] start $(date -u +%FT%TZ)" | tee -a "$LOG"

RESULT=$(./.venv/bin/python -c "
import asyncio, json
from providers.calendar import CalendarProvider
r = asyncio.run(CalendarProvider().fetch())
print(json.dumps({'name':'calendar','status':r.status,'data':r.data,'error':r.error}))
")
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$RESULT" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[calendar] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"

# Fail-fast: if gws-cli hung at OAuth-prompt (refresh-token expired),
# stderr in the log will contain the signature. Detect immediately and
# surface a clear error instead of letting a 90s hang repeat next time.
# 2026-07-09 — see skill pult-calendar-gwscli-hang-pitfall.
if grep -q "Google OAuth Authorization Required\|127.0.0.1:8081" "$LOG"; then
  echo "[calendar] FATAL: gws-cli needs re-auth — see skill pult-calendar-gwscli-hang-pitfall" >&2
  echo "[calendar] hint: sudo env -i HOME=/root/.hermes/profiles/developer/home ... gws-cli auth login" >&2
  exit 2
fi

exit $RC