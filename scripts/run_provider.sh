#!/bin/bash
# /root/morning_brief_v2/scripts/run_provider.sh
# Unified wrapper invoked by /etc/cron.d/morning-brief-v2.
#   $1 = leaf name (e.g. run_garmin_cron.sh)
#
# Per cron prompt instruction (2026-06-28):
#   - Capture exit code WITHOUT set -e (so non-zero is visible to caller)
#   - Tail log to alerts on failure
#   - Exit with the leaf's exit code (don't mask it)
#
# Built 2026-06-29 — previously missing on disk, causing silent failures of
# the 06:30 collect stage (cron file referenced run_provider.sh + 6 leaves,
# none of which existed; only run_garmin_cron.sh was later added).
#
# 2026-07-03: added `timeout 180s` around leaf execution. Without it,
# a hung Google OAuth refresh (calendar) left run_provider.sh zombies
# in the cgroup for days — the cron line fired every morning but the
# new run_provider spawned alongside the old hung one. 180s covers
# Garmin Connect login+fetch (<90s), Calendar OAuth refresh (<30s),
# Todoist/weather/food (<10s each). If a leaf times out we log it and
# exit 124 (GNU timeout convention) so the alert path treats it as a
# hard failure.

set -uo pipefail   # NO set -e — we need to see the leaf's exit code

LEAF="$1"
LEAF_PATH="/root/morning_brief_v2/scripts/$LEAF"
LEAF_TIMEOUT="${LEAF_TIMEOUT:-180}"  # seconds; override per-leaf via env

if [ -z "$LEAF" ] || [ ! -x "$LEAF_PATH" ]; then
  echo "[run_provider] $LEAF missing or not executable" >&2
  exit 127
fi

if ! command -v timeout >/dev/null 2>&1; then
  echo "[run_provider] 'timeout' command not found — refusing to run without a deadline" >&2
  exit 126
fi

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/${LEAF%.sh}-$DATE_UTC.log"

echo "[run_provider] start leaf=$LEAF timeout=${LEAF_TIMEOUT}s $(date -u +%FT%TZ)" >> "$LOG"
timeout "${LEAF_TIMEOUT}s" "$LEAF_PATH" >> "$LOG" 2>&1
RC=$?
echo "[run_provider] end leaf=$LEAF rc=$RC $(date -u +%FT%TZ)" >> "$LOG"

if [ "$RC" -ne 0 ]; then
  echo "[run_provider] $LEAF failed rc=$RC — tail of log:" >&2
  tail -30 "$LOG" >&2
  if [ "$RC" -eq 124 ]; then
    echo "[run_provider] $LEAF timed out after ${LEAF_TIMEOUT}s" >> "$LOG"
  fi
  # Optional: call notify_failure.sh if/when it exists.
  # [ -x /root/morning_brief_v2/scripts/notify_failure.sh ] && \
  #   /root/morning_brief_v2/scripts/notify_failure.sh "$LEAF" "$RC" "$LOG"
fi

exit $RC