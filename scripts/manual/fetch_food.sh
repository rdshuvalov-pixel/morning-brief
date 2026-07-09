#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_food.sh
# Parse food-log.md for yesterday's entries → food_log (food_date = today-1).

set -eo pipefail
cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/food-$(date -u +%Y-%m-%dT%H%M).log"

echo "[food] start $(date -u +%FT%TZ)" | tee -a "$LOG"

RESULT=$(./.venv/bin/python -c "
import asyncio, json
from providers.food import FoodProvider
r = asyncio.run(FoodProvider().fetch())
print(json.dumps({'name':'food','status':r.status,'data':r.data,'error':r.error}))
")
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$RESULT" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[food] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"
exit $RC