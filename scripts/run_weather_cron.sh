#!/bin/bash
# /root/morning_brief_v2/scripts/run_weather_cron.sh
# Daily Weather fetch + DB upsert for TODAY. Invoked by run_provider.sh
# from /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# 2026-07-05: bug A fix — previously this script only fetched the provider
# and printed status to log, never writing to Supabase. Pre-flight at 08:30
# render time then saw 0 rows in weather_log for the target date and the
# render retry loop blew the Hermes 120s budget. Now we JSON-serialize
# the ProviderResult and feed it to scripts/_write_provider.py.

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

# Step 1: fetch — emit JSON line to stdout (captured into RESULT).
RESULT=$(./.venv/bin/python - <<'PY' 2>>"$LOG"
import asyncio, json
from providers.weather import WeatherProvider
r = asyncio.run(WeatherProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error}")
print("WRITE_JSON_BEGIN")
print(json.dumps({"name": "weather", "status": r.status,
                  "data": r.data, "error": r.error}))
PY
)
echo "$RESULT" >> "$LOG"

# Step 2: parse JSON line and hand it to the shared writer.
PAYLOAD=$(echo "$RESULT" | sed -n '/^WRITE_JSON_BEGIN$/{n;p;}')
if [ -z "$PAYLOAD" ]; then
    echo "[weather] writer: no JSON payload fetched — DB write skipped" >> "$LOG"
    echo "[weather] end rc=1 $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$PAYLOAD" >> "$LOG" 2>&1
WRITE_RC=$?
echo "[weather] writer rc=$WRITE_RC" >> "$LOG"

echo "[weather] end rc=$WRITE_RC $(date -u +%FT%TZ)" >> "$LOG"
exit $WRITE_RC