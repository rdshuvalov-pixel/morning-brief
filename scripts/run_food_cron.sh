#!/bin/bash
# /root/morning_brief_v2/scripts/run_food_cron.sh
# Daily Food log fetch + DB upsert (yesterday). Invoked by run_provider.sh
# from /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# 2026-07-05: bug A fix — previously fetch-only, never wrote to Supabase.
# Food's "no entries for yesterday" is normal on most days (user hasn't
# logged yet), so we treat status=unavailable as rc=0 and skip the write.

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

RESULT=$(./.venv/bin/python - <<'PY' 2>>"$LOG"
import asyncio, json
from providers.food import FoodProvider
r = asyncio.run(FoodProvider().fetch())
entries = (r.data or {}).get('entries', []) if isinstance(r.data, dict) else []
print(f"status={r.status} source={r.source} err={r.error} entries={len(entries)}")
# Only emit a JSON payload if there's data — empty food_log is fine.
if r.status == "ok" and entries:
    print("WRITE_JSON_BEGIN")
    print(json.dumps({"name": "food", "status": r.status,
                      "data": r.data, "error": r.error}))
PY
)
echo "$RESULT" >> "$LOG"

PAYLOAD=$(echo "$RESULT" | sed -n '/^WRITE_JSON_BEGIN$/{n;p;}')
if [ -z "$PAYLOAD" ]; then
    echo "[food] writer: no entries for yesterday — DB write skipped (normal)" >> "$LOG"
    echo "[food] end rc=0 $(date -u +%FT%TZ)" >> "$LOG"
    exit 0
fi
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$PAYLOAD" >> "$LOG" 2>&1
WRITE_RC=$?
echo "[food] writer rc=$WRITE_RC" >> "$LOG"

echo "[food] end rc=$WRITE_RC $(date -u +%FT%TZ)" >> "$LOG"
exit $WRITE_RC