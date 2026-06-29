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

set -uo pipefail   # NO set -e — we need to see the leaf's exit code

LEAF="$1"
LEAF_PATH="/root/morning_brief_v2/scripts/$LEAF"

if [ -z "$LEAF" ] || [ ! -x "$LEAF_PATH" ]; then
  echo "[run_provider] $LEAF missing or not executable" >&2
  exit 127
fi

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/${LEAF%.sh}-$DATE_UTC.log"

"$LEAF_PATH" >> "$LOG" 2>&1
RC=$?

if [ "$RC" -ne 0 ]; then
  echo "[run_provider] $LEAF failed rc=$RC — tail of log:" >&2
  tail -30 "$LOG" >&2
  # Optional: call notify_failure.sh if/when it exists.
  # [ -x /root/morning_brief_v2/scripts/notify_failure.sh ] && \
  #   /root/morning_brief_v2/scripts/notify_failure.sh "$LEAF" "$RC" "$LOG"
fi

exit $RC