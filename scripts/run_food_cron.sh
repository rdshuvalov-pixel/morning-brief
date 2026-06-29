#!/bin/bash
# /root/morning_brief_v2/scripts/run_food_cron.sh
# Daily Food log fetch (yesterday, by default of FoodProvider).
# Invoked by run_provider.sh from /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.

set -eo pipefail

cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/food-$DATE_UTC.log"

echo "[food] start $(date -u +%FT%TZ)" >> "$LOG"

./.venv/bin/python - <<'PY' >> "$LOG" 2>&1
import asyncio
from providers.food import FoodProvider
r = asyncio.run(FoodProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error} entries={len((r.data or {}).get('entries', []))}")
PY
RC=$?

echo "[food] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC