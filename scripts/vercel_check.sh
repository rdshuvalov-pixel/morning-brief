#!/bin/bash
# /root/morning_brief_v2/scripts/vercel_check.sh
# Verify Vercel deployment is live and serving the right content.
# Returns 0 on success, 1 on mismatch/timeout.
#
# Required env: VERCEL_DEPLOY_URL (defaults to https://rus-morning-brief.vercel.app)

set -uo pipefail

URL="${VERCEL_DEPLOY_URL:-https://rus-morning-brief.vercel.app}"
EXPECTED_DATE="${1:-}"   # YYYY-MM-DD; if empty, parse from brief_today.html

LOCAL_FILE="/root/morning_brief_v2/web/brief_today.html"

if [ -z "$EXPECTED_DATE" ]; then
    if [ -f "$LOCAL_FILE" ]; then
        # Parse Lisbon date from <title>Morning Brief — 2 July</title>
        # Use the local file's modtime as a fallback YYYY-MM-DD
        EXPECTED_DATE=$(date -r "$LOCAL_FILE" +%Y-%m-%d)
    fi
fi

if [ -z "$EXPECTED_DATE" ]; then
    echo "[vercel_check] no EXPECTED_DATE, aborting" >&2
    exit 1
fi

# Probe the live URL with cache-busting query string.
# Use the Lisbon date as ?date= param so the page-side JS picks it up
# (but Vercel serves the same static index.html regardless).
PROBE_URL="${URL}/?date=${EXPECTED_DATE}&_=$(date +%s)"
TMP_HTML=$(mktemp)
trap 'rm -f "$TMP_HTML"' EXIT

# Get HTTP status and Last-Modified
HEADERS=$(curl -sS -D - -o "$TMP_HTML" -A "Mozilla/5.0" \
    -H "Cache-Control: no-cache" --max-time 30 "$PROBE_URL" 2>&1)
HTTP_CODE=$(echo "$HEADERS" | head -1 | awk '{print $2}')
LAST_MOD=$(echo "$HEADERS" | grep -i "^last-modified:" | head -1 | sed 's/^[Ll]ast-[Mm]odified: //' | tr -d '\r')
LIVE_TITLE=$(grep -oE '<title>[^<]+</title>' "$TMP_HTML" 2>/dev/null | head -1)

echo "[vercel_check] URL=$PROBE_URL"
echo "[vercel_check] HTTP=$HTTP_CODE Last-Modified=$LAST_MOD"
echo "[vercel_check] live title: $LIVE_TITLE"
echo "[vercel_check] expected date fragment: $EXPECTED_DATE"

if [ "$HTTP_CODE" != "200" ]; then
    echo "[vercel_check] FAIL: HTTP=$HTTP_CODE" >&2
    exit 1
fi

# Title should contain "Morning Brief" AND the expected date (YYYY-MM-DD
# or the human "3 July" form). 2026-07-03: previous check only verified
# the brand string, which let a stale 2-July title pass after a rerender.
EXPECTED_COMPACT=$(echo "$EXPECTED_DATE" | awk -F- '{print $3}' | sed 's/^0//')
EXPECTED_MONTH_NUM=$(echo "$EXPECTED_DATE" | awk -F- '{print $2}' | sed 's/^0//')
TITLE_OK=0
if echo "$LIVE_TITLE" | grep -qi "Morning Brief"; then
    # Match the day number and either the month name or the full ISO date
    if echo "$LIVE_TITLE" | grep -qE "(^|[^0-9])${EXPECTED_COMPACT} [^A-Za-z0-9]?(January|February|March|April|May|June|July|August|September|October|November|December)|${EXPECTED_DATE}"; then
        TITLE_OK=1
    fi
fi
if [ "$TITLE_OK" -ne 1 ]; then
    echo "[vercel_check] FAIL: title '$LIVE_TITLE' does not contain expected date $EXPECTED_DATE" >&2
    exit 1
fi

echo "[vercel_check] OK"
exit 0