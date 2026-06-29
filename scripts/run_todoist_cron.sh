#!/bin/bash
# /root/morning_brief_v2/scripts/run_todoist_cron.sh
# Daily Todoist fetch for TODAY. Invoked by run_provider.sh from
# /etc/cron.d/morning-brief-v2 at 06:30 Lisbon.

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

./.venv/bin/python - <<'PY' >> "$LOG" 2>&1
import asyncio
from providers.todoist import TodoistProvider
r = asyncio.run(TodoistProvider().fetch())
print(f"status={r.status} source={r.source} err={r.error} tasks={len((r.data or {}).get('tasks', []))}")
PY
RC=$?

echo "[todoist] end rc=$RC $(date -u +%FT%TZ)" >> "$LOG"
exit $RC