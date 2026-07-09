#!/bin/bash
# /root/morning_brief_v2/pult/worker_test.sh
# Smoke-test the pult worker via manual INSERT into morning_brief_v2.jobs.
# Requires: pult/schema.sql already executed in Supabase, mbrief-pult-worker.service running.
#
# Usage: bash pult/worker_test.sh

set -euo pipefail
ENV_FILE="/root/morning_brief_v2/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found"; exit 1
fi
. "$ENV_FILE"

SUPABASE_URL="${SUPABASE_URL:?not set}"
SUPABASE_KEY="${SUPABASE_KEY:?not set}"
DATE="$(TZ=Europe/Lisbon date +%F)"

echo "=== Worker smoke test ==="
echo "Target: weather @ $DATE"

# Sanity probe: can we even reach Supabase?
n=$(curl -sG -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" \
       -H "Accept-Profile: morning_brief_v2" \
       "$SUPABASE_URL/rest/v1/jobs" \
       --data-urlencode "select=id" --data-urlencode "limit=1" \
     | (python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "ERR"))
echo "Supabase reachability (jobs table): $n row(s) returned (0 = reachable + empty)"

# Try to insert a weather job
JOB_ID=$(curl -s -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" \
  -H "Content-Type: application/json" \
  -H "Content-Profile: morning_brief_v2" \
  -H "Prefer: return=representation" \
  -d "{\"script\":\"weather\",\"payload\":{\"date\":\"$DATE\"}}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 'EMPTY')")
echo "Inserted job id=$JOB_ID"

if [ "$JOB_ID" = "EMPTY" ]; then
  echo "FAIL: insert returned empty (Supabase error or table missing)"; exit 1
fi

# Poll status up to 30s
echo "Polling status (up to 30s)..."
STATE=""
for i in 1 2 3 4 5 6; do
  sleep 5
  STATE=$(curl -sG -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" \
              -H "Accept-Profile: morning_brief_v2" \
              "$SUPABASE_URL/rest/v1/jobs" \
              --data-urlencode "select=status" --data-urlencode "id=eq.$JOB_ID" \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['status'] if d else 'NONE')")
  echo "  t=${i}*5s state=$STATE"
  [ "$STATE" = "done" ] && break
  [ "$STATE" = "failed" ] && { echo "FAIL: worker reported failure"; exit 1; }
done

[ "$STATE" = "done" ] || { echo "FAIL: state=$STATE after 30s (worker not running?)"; exit 1; }
echo "PASS: worker picked up and completed job in <30s"
