#!/bin/bash
# /root/morning_brief_v2/scripts/run_garmin_cron.sh
# Daily Garmin fetch for TODAY (fresh morning data: sleep/HRV/RHR/BB).
# Invoked by /etc/cron.d/morning-brief-v2 at 07:00 CEST (06:00 Lisbon).
#
# Fix (variant A, 2026-07-02): was writing YESTERDAY (closed day), which
# conflicted with render at 08:30 that wrote TODAY (live morning). This
# produced two different garmin_metrics rows for the same brief period.
# Now we write TODAY only, so render at 08:30 just reads what's already
# in the DB. Idempotent via on_conflict=date upsert.

set -eo pipefail

cd /root/morning_brief_v2

# Load env
set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/garmin-$DATE_UTC.log"

# Cron runs at 07:00 CEST (06:00 Lisbon). Compute "today" in host TZ.
TODAY=$(date +%Y-%m-%d)

echo "[garmin] start $(date -u +%FT%TZ) target=$TODAY" >> "$LOG"

./.venv/bin/python /root/morning_brief_v2/run_garmin.py --date "$TODAY" >> "$LOG" 2>&1
RC=$?

echo "[garmin] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC
