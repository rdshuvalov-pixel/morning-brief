#!/bin/bash
# /root/morning_brief_v2/scripts/run_garmin_cron.sh
# Daily Garmin fetch for both TODAY and YESTERDAY.
# Invoked by /etc/cron.d/morning-brief-v2 at 07:00 CEST (06:00 Lisbon).
#
# Contract (2026-07-03): each morning's cron run MUST leave BOTH rows in
# garmin_metrics:
#   - date=today     → live data at fetch time (steps/kcal accumulate
#                      during the day, sleep/HRV/RHR/BB settle overnight)
#   - date=yesterday → settlement data (Garmin API returns final numbers
#                      the day after, not in real-time)
#
# Why both: render_playful.py:676 reads `garmin_yesterday` for the
# "movement" block. If yesterday's row is missing or wrong, the brief
# shows stale or yesterday-morning-settlement values that don't match the
# user's actual day.
#
# 2026-07-02: was writing YESTERDAY (closed day). Caused conflict with the
#             08:30 render that wrote TODAY (live morning) → two different
#             garmin_metrics rows for the same brief period.
# 2026-07-03: switch to writing both. run_garmin.py defaults to
#             `--also-yesterday=true` when no --date is given.

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

echo "[garmin] start $(date -u +%FT%TZ) target=today+yesterday" >> "$LOG"

# No --date: run_garmin.py defaults to today + yesterday.
./.venv/bin/python /root/morning_brief_v2/run_garmin.py >> "$LOG" 2>&1
RC=$?

echo "[garmin] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC
