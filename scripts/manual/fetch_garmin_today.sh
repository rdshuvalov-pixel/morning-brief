#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_garmin_today.sh
# Fetch Garmin metrics for TODAY and upsert into garmin_metrics.
#
# Behaviour (per user 2026-07-08, deliberate override of run_garmin.py's
# settled-drop logic):
#   - target date = today (Europe/Lisbon)
#   - writes ALL fields Garmin returns — including morning BB peak, HRV
#     overnight, sleep duration/score/deep_pct, RHR, SpO2, training_readiness,
#     stress — plus intraday live (steps/distance/kcal).
#   - NO settled-drop, even though the day is unclosed. This is intentional:
#     Garmin's "morning settled" numbers (BB/HRV/sleep) are what the user
#     wants to see for today's brief.
#
# Pairs with: fetch_garmin_yesterday.sh (writes YESTERDAY only).
#
# Usage:
#   ./fetch_garmin_today.sh                    # default: today
#   ./fetch_garmin_today.sh --date 2026-07-08  # override (rare)

set -eo pipefail
cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
STAMP=$(date -u +%Y-%m-%dT%H%M)
LOG="$LOG_DIR/garmin_today-$STAMP.log"

echo "[garmin_today] start $(date -u +%FT%TZ)" | tee -a "$LOG"

# Pass through any CLI args (e.g. --date override). run_garmin_today.py
# handles parsing and defaults to today.
./.venv/bin/python /root/morning_brief_v2/run_garmin_today.py "$@" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}

echo "[garmin_today] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"
exit $RC