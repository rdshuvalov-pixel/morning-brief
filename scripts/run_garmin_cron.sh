#!/bin/bash
# /root/morning_brief_v2/scripts/run_garmin_cron.sh
# Daily Garmin fetch for YESTERDAY (date.today() - 1).
# Invoked by /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# Note: run_garmin.py defaults to TODAY, but for the 06:30 cron we want
# yesterday's data (sleep/RHR/HRV from the just-finished night). This
# wrapper computes "yesterday" relative to the host's local date.

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

# Cron runs at 06:30 Lisbon (UTC+0 in winter / UTC+1 in summer).
# Compute "yesterday" from the host's clock (set to Europe/Lisbon in cron).
YESTERDAY=$(date -d 'yesterday' +%Y-%m-%d)

echo "[garmin] start $(date -u +%FT%TZ) target=$YESTERDAY" >> "$LOG"

./.venv/bin/python /root/morning_brief_v2/run_garmin.py --date "$YESTERDAY" >> "$LOG" 2>&1
RC=$?

echo "[garmin] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC
