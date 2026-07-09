#!/bin/bash
# /root/morning_brief_v2/scripts/run_todoist_cron.sh
# Daily Todoist fetch + DB upsert for TODAY. Invoked by run_provider.sh
# from /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.
#
# 2026-07-05: bug A fix — previously fetch-only, never wrote to Supabase.

set -eo pipefail

cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/todoist-$DATE_UTC.log"

echo "[todoist] start $(date -u +%FT%TZ)" >> "$LOG"

RESULT=$(./.venv/bin/python - <<'PY' 2>>"$LOG"
import asyncio, json
from providers.todoist import TodoistProvider
r = asyncio.run(TodoistProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error} tasks={len((r.data or {}).get('tasks', []))}")
print("WRITE_JSON_BEGIN")
print(json.dumps({"name": "tasks", "status": r.status,
                  "data": r.data, "error": r.error}))
PY
)
echo "$RESULT" >> "$LOG"

PAYLOAD=$(echo "$RESULT" | sed -n '/^WRITE_JSON_BEGIN$/{n;p;}')
if [ -z "$PAYLOAD" ]; then
    echo "[todoist] writer: no JSON payload fetched — DB write skipped" >> "$LOG"
    echo "[todoist] end rc=1 $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$PAYLOAD" >> "$LOG" 2>&1
WRITE_RC=$?
echo "[todoist] writer rc=$WRITE_RC" >> "$LOG"

echo "[todoist] end rc=$WRITE_RC $(date -u +%FT%TZ)" >> "$LOG"
exit $WRITE_RC