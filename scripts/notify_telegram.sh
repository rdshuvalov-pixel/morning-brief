#!/bin/bash
# /root/morning_brief_v2/scripts/notify_telegram.sh
# Send a message to Telegram via bot token from .env.
# Usage: notify_telegram.sh "message text"
# Or:   notify_telegram.sh --chat <chat_id> "message text"

set -uo pipefail

# Load .env if present
ENV_FILE="/root/morning_brief_v2/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

CHAT_ID="${MB2_ALERT_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
TEXT="${1:-}"
shift 1 || true

while [ $# -gt 0 ]; do
    case "$1" in
        --chat) CHAT_ID="$2"; shift 2 ;;
        *) TEXT="$TEXT $1"; shift ;;
    esac
done

if [ -z "$TEXT" ] || [ -z "$CHAT_ID" ]; then
    echo "[notify_telegram] missing text or chat_id (chat=$CHAT_ID text='$TEXT')" >&2
    exit 1
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "[notify_telegram] no TELEGRAM_BOT_TOKEN in .env" >&2
    exit 1
fi

# Send via curl (URL-encode the text)
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'chat_id': '$CHAT_ID', 'text': sys.argv[1], 'parse_mode': 'HTML'}))" "$TEXT")
HTTP_CODE=$(curl -sS -o /tmp/tg_resp.json -w "%{http_code}" \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    --max-time 30)

if [ "$HTTP_CODE" != "200" ]; then
    echo "[notify_telegram] FAIL HTTP=$HTTP_CODE body=$(cat /tmp/tg_resp.json | head -c 200)" >&2
    exit 1
fi
echo "[notify_telegram] sent to $CHAT_ID"