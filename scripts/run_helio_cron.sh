#!/bin/bash
# /root/morning_brief_v2/scripts/run_helio_cron.sh
# Helio disabled 2026-06-29 — браслет больше не носим, данные не нужны.
# This is a NO-OP wrapper that preserves the cron-fan-out structure
# (/etc/cron.d/morning-brief-v2 references all 6 leaves).
#
# If you re-enable Helio later, replace this with a real provider-call
# (see run_weather_cron.sh for the inline-python pattern) AND uncomment
# the "helio" key in run_all.py provider_factories.

set -uo pipefail   # not -e: we want to log the disabled state, not fail

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/helio-$DATE_UTC.log"

echo "[helio] disabled 2026-06-29 — skipping $(date -u +%FT%TZ)" >> "$LOG"
exit 0