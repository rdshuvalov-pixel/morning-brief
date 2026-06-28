#!/bin/bash
# /root/morning_brief_v2/scripts/render_brief_cron.sh
# Hermes cron wrapper — START render in background, exit 0 immediately.
# Why: Hermes cron watchdog has 120s timeout, render takes 60-120s.
# The watchdog job is just a kicker — actual render runs detached, writes
# to logs/cron/render-<date>.log, copies to web/index.html.
#
# Per operator @appelcien 2026-06-28: decoupling — cron failure doesn't
# break archive (07:30 job picks whatever's in brief_today.html).

set -eo pipefail
cd /root/morning_brief_v2

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/render-$DATE_UTC.log"

# Kick the actual render in background. The bash subshell detaches from
# the cron process tree via nohup + </dev/null + &, so the cron watchdog
# can exit 0 immediately while render continues.
nohup bash -c '
  set -eo pipefail
  cd /root/morning_brief_v2
  set -a; . ./.env; set +a
  echo "[render] bg start $(date -u +%FT%TZ)"
  ./.venv/bin/python run_all.py --out web/brief_today.html 2>&1
  cp web/brief_today.html web/index.html
  echo "[render] bg done $(date -u +%FT%TZ)"
' >> "$LOG" 2>&1 < /dev/null &
BG_PID=$!
disown $BG_PID 2>/dev/null || true

echo "[render] kicked bg pid=$BG_PID at $(date -u +%FT%TZ)" >> "$LOG"
echo "render kicked (bg pid=$BG_PID, log=$LOG)"
exit 0