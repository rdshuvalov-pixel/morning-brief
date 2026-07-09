#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_garmin_yesterday.sh
# Fetch Garmin metrics for YESTERDAY and upsert into garmin_metrics.
#
# Behaviour (per user 2026-07-08, replacement for the old combined fetch_garmin.sh):
#   - target date = yesterday (Europe/Lisbon)
#   - day is closed → Garmin returns all settled fields, written verbatim
#   - NO settled-drop (yesterday is always closed)
#
# Pairs with: fetch_garmin_today.sh (which writes TODAY with NO settled-drop).
#
# Usage:
#   ./fetch_garmin_yesterday.sh                    # default: yesterday
#   ./fetch_garmin_yesterday.sh --date 2026-07-05  # specific date (closed day)

set -eo pipefail
cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
STAMP=$(date -u +%Y-%m-%dT%H%M)
LOG="$LOG_DIR/garmin_yesterday-$STAMP.log"

# Resolve yesterday in Lisbon (covers CEST/UTC offset cleanly).
DATE_YESTERDAY=$(TZ=Europe/Lisbon date -d 'yesterday' +%F)

echo "[garmin_yesterday] start $(date -u +%FT%TZ) date=$DATE_YESTERDAY" | tee -a "$LOG"

./.venv/bin/python /root/morning_brief_v2/run_garmin.py \
    --date "$DATE_YESTERDAY" \
    --no-also-yesterday 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}

echo "[garmin_yesterday] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"
exit $RC