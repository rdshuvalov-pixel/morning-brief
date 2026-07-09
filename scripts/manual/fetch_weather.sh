#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_weather.sh
# Fetch weather forecast → weather_log (today, 3 periods: morning/day/evening).

set -eo pipefail
cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weather-$(date -u +%Y-%m-%dT%H%M).log"

echo "[weather] start $(date -u +%FT%TZ)" | tee -a "$LOG"

RESULT=$(./.venv/bin/python -c "
import asyncio, json
from providers.weather import WeatherProvider
r = asyncio.run(WeatherProvider().fetch())
print(json.dumps({'name':'weather','status':r.status,'data':r.data,'error':r.error}))
")
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$RESULT" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[weather] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"
exit $RC