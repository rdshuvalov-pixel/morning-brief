#!/bin/bash
# /root/morning_brief_v2/scripts/render_with_retry.sh
# Run preflight_check → run_all.py → archive + git push → vercel_check.
# On failure: alert to Telegram and retry up to 3 times.
#
# Usage:
#     ./scripts/render_with_retry.sh                    # render today
#     ./scripts/render_with_retry.sh 2026-07-02         # specific date

set -uo pipefail

DATE="${1:-}"
MAX_RETRIES="${MAX_RETRIES:-3}"
LOG="/root/morning_brief_v2/logs/cron/render_with_retry-$(date -u +%Y-%m-%d).log"
mkdir -p "$(dirname "$LOG")"

cd /root/morning_brief_v2
set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

log() { echo "[render_with_retry] $(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
fail_count=0
final_status="UNKNOWN"

for attempt in $(seq 1 "$MAX_RETRIES"); do
    log "===== attempt $attempt/$MAX_RETRIES ====="

    # Step 1: preflight (skip on retry; if data was OK first time, retry won't help)
    if [ "$attempt" = "1" ]; then
        if [ -n "$DATE" ]; then
            PF_OUT=$(./.venv/bin/python preflight_check.py --date "$DATE" 2>&1)
        else
            PF_OUT=$(./.venv/bin/python preflight_check.py 2>&1)
        fi
        PF_RC=$?
        log "preflight rc=$PF_RC"
        echo "$PF_OUT" >> "$LOG"
        if [ "$PF_RC" = "2" ]; then
            log "preflight FAIL: data missing — waiting 60s and retrying"
            sleep 60
            continue
        elif [ "$PF_RC" != "0" ]; then
            log "preflight FAIL: infrastructure problem (rc=$PF_RC)"
            ./scripts/notify_telegram.sh "⚠️ morning-brief preflight infrastructure failure (rc=$PF_RC). Last 3 lines: $(echo "$PF_OUT" | tail -3 | tr '\n' ' ')" 2>&1 | tee -a "$LOG"
            final_status="PREFLIGHT_INFRA_FAIL"
            break
        fi
    fi

    # Step 2: run_all.py (render + DB writes)
    if [ -n "$DATE" ]; then
        RENDER_OUT=$(./.venv/bin/python run_all.py --date "$DATE" 2>&1)
    else
        RENDER_OUT=$(./.venv/bin/python run_all.py 2>&1)
    fi
    RENDER_RC=$?
    log "run_all rc=$RENDER_RC"
    echo "$RENDER_OUT" >> "$LOG"
    if [ "$RENDER_RC" != "0" ]; then
        log "run_all FAILED rc=$RENDER_RC — retrying"
        fail_count=$((fail_count+1))
        sleep 30
        continue
    fi

    # Step 3: archive + git push + vercel_check (delegate to archive_brief_cron.sh logic)
    ARCHIVE_OUT=$(./scripts/archive_brief_cron.sh 2>&1)
    ARCHIVE_RC=$?
    log "archive_brief_cron rc=$ARCHIVE_RC"
    echo "$ARCHIVE_OUT" >> "$LOG"
    if [ "$ARCHIVE_RC" != "0" ]; then
        log "archive_brief_cron FAILED rc=$ARCHIVE_RC — retrying"
        fail_count=$((fail_count+1))
        sleep 30
        continue
    fi

    # Step 4: final vercel_check
    if [ -n "$DATE" ]; then
        VC_OUT=$(./scripts/vercel_check.sh "$DATE" 2>&1)
    else
        VC_OUT=$(./scripts/vercel_check.sh 2>&1)
    fi
    VC_RC=$?
    log "vercel_check rc=$VC_RC"
    echo "$VC_OUT" >> "$LOG"
    if [ "$VC_RC" != "0" ]; then
        log "vercel_check FAILED rc=$VC_RC — retrying"
        fail_count=$((fail_count+1))
        sleep 30
        continue
    fi

    log "ALL STEPS OK on attempt $attempt"
    final_status="OK"
    break
done

if [ "$final_status" != "OK" ]; then
    log "GIVING UP after $MAX_RETRIES attempts (status=$final_status, fail_count=$fail_count)"
    ./scripts/notify_telegram.sh "🚨 morning-brief: ALL $MAX_RETRIES attempts failed. status=$final_status, fail_count=$fail_count. Manual check: https://rus-morning-brief.vercel.app /logs/cron/render_with_retry-$(date -u +%Y-%m-%d).log" 2>&1 | tee -a "$LOG"
    exit 1
fi

log "render_with_retry done, final_status=OK"
exit 0