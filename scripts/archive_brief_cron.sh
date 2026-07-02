#!/bin/bash
# /root/morning_brief_v2/scripts/archive_brief_cron.sh
# Daily brief snapshot — invoked by /etc/cron.d/morning-brief-render at 07:30 Lisbon.
#
# Pipeline:
#   1. Copy web/brief_today.html → web/archive/<lisbon-date>.html
#   2. Update web/archive/manifest.json
#   3. git add + commit + push (so Vercel auto-deploys archive URLs)
#
# Per operator @appelcien 2026-06-28: snapshot at 07:30 (separate from
# 07:00 render). If render failed, brief_today.html is the last good
# copy — archive still works (idempotent). If push fails, archive lives
# locally and gets retried next day.

set -eo pipefail

cd /root/morning_brief_v2

# Load env
set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/cron"
mkdir -p "$LOG_DIR"
DATE_UTC=$(date -u +%Y-%m-%d)
LOG="$LOG_DIR/archive-$DATE_UTC.log"

echo "[archive] start $(date -u +%FT%TZ)" >> "$LOG"

# Step 1+2: snapshot to archive/ + write manifest.json
if ! ./.venv/bin/python archive_brief.py >> "$LOG" 2>&1; then
    echo "[archive] FAIL at archive_brief.py $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi

# Step 3: commit + push
#         (only if there's something new to commit)
git add web/archive/ web/index.html
if git diff --cached --quiet; then
    echo "[archive] nothing to commit $(date -u +%FT%TZ)" >> "$LOG"
    exit 0
fi

if ! git -c user.email=hermes@developer -c user.name=Hermes \
        commit -m "Archive brief $(TZ=Europe/Lisbon date +%Y-%m-%d)" >> "$LOG" 2>&1; then
    echo "[archive] FAIL at git commit $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi

if ! git push origin main >> "$LOG" 2>&1; then
    echo "[archive] FAIL at git push $(date -u +%FT%TZ)" >> "$LOG"
    exit 1
fi

echo "[archive] git push done $(date -u +%FT%TZ)" >> "$LOG"

# Step 4: verify Vercel deployment is live with the new content
# Wait up to 60s for Vercel to deploy (typical: 10-30s)
VERCEL_OK=0
for i in 1 2 3 4 5 6; do
    sleep 10
    if /root/morning_brief_v2/scripts/vercel_check.sh >> "$LOG" 2>&1; then
        VERCEL_OK=1
        echo "[archive] vercel_check PASSED on attempt $i $(date -u +%FT%TZ)" >> "$LOG"
        break
    fi
    echo "[archive] vercel_check attempt $i failed, retrying..." >> "$LOG"
done

if [ "$VERCEL_OK" -ne 1 ]; then
    echo "[archive] FAIL: vercel never showed new content after 60s $(date -u +%FT%TZ)" >> "$LOG"
    # Telegram alert (per /etc/cron.d MB2_ALERTS=1)
    if [ "${MB2_ALERTS:-0}" = "1" ] && [ -n "${MB2_ALERT_CHAT_ID:-}" ]; then
        /root/morning_brief_v2/scripts/notify_telegram.sh \
            "⚠️ morning-brief-archive: git push OK, but Vercel never showed new content for 60s. Manual check: https://rus-morning-brief.vercel.app" \
            >> "$LOG" 2>&1 || true
    fi
    exit 1
fi

echo "[archive] done $(date -u +%FT%TZ)" >> "$LOG"
exit 0