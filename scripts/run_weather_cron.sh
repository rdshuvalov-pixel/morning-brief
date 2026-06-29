#!/bin/bash
# /root/morning_brief_v2/scripts/run_weather_cron.sh
# Daily Weather fetch for TODAY. Invoked by run_provider.sh from
# /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# No run_weather.py exists — providers are called directly per the
# morning-brief-v2 skill pattern ("If even the per-provider runner is
# missing, call the provider class directly").

set -eo pipefail

cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/weather-$DATE_UTC.log"

echo "[weather] start $(date -u +%FT%TZ)" >> "$LOG"

./.venv/bin/python - <<'PY' >> "$LOG" 2>&1
import asyncio
from providers.weather import WeatherProvider
r = asyncio.run(WeatherProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error}")
PY
RC=$?

echo "[weather] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC