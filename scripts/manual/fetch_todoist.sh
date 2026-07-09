#!/bin/bash
# /root/morning_brief_v2/scripts/manual/fetch_todoist.sh
# Fetch Todoist tasks (today | overdue) → tasks.

set -eo pipefail
cd /root/morning_brief_v2

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/todoist-$(date -u +%Y-%m-%dT%H%M).log"

echo "[todoist] start $(date -u +%FT%TZ)" | tee -a "$LOG"

RESULT=$(./.venv/bin/python -c "
import asyncio, json
from providers.todoist import TodoistProvider
r = asyncio.run(TodoistProvider().fetch())
print(json.dumps({'name':'tasks','status':r.status,'data':r.data,'error':r.error}))
")
./.venv/bin/python /root/morning_brief_v2/scripts/_write_provider.py "$RESULT" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[todoist] end rc=$RC $(date -u +%FT%TZ)" | tee -a "$LOG"
exit $RC