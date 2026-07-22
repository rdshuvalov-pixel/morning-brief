#!/bin/bash
# /root/morning_brief_v2/scripts/manual/archive_and_publish.sh
# Three steps:
#   1. Render brief HTML from DB → web/archive/<date>.html
#   2. Copy archive → web/brief_today.html + web/index.html
#   3. git add + commit + push (Vercel auto-deploys)
#
# Usage:
#   ./archive_and_publish.sh                       # default: today (Lisbon)
#   ./archive_and_publish.sh --date 2026-07-07     # explicit date
#   ./archive_and_publish.sh --date 2026-07-07 --skip-push   # local only

set -eo pipefail
cd /root/morning_brief_v2

DATE="${1:-$(TZ=Europe/Lisbon date +%Y-%m-%d)}"
# Allow --date YYYY-MM-DD form
if [ "$1" = "--date" ] && [ -n "$2" ]; then
  DATE="$2"
  SKIP_PUSH=0
  if [ "$3" = "--skip-push" ]; then SKIP_PUSH=1; fi
else
  SKIP_PUSH=0
  case "${3:-}" in --skip-push) SKIP_PUSH=1 ;; esac
fi

set -a
if [ -f ./.env ]; then . ./.env; fi
set +a

LOG_DIR="/root/morning_brief_v2/logs/manual"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/publish-$DATE.log"

echo "[publish] start date=$DATE $(date -u +%FT%TZ)" | tee -a "$LOG"

# Step 1: rerender archive/<date>.html from DB
# The LLM button is the canonical writer for briefs.narrative + five
# briefs.narrative_* columns. Publishing must not trigger another LLM run:
# it only renders the already-populated DB row and fails visibly if that row
# is incomplete.
echo "[publish] preflight: verify complete narrative row for $DATE" | tee -a "$LOG"
if ! ./.venv/bin/python scripts/manual/verify_llm_row.py --date "$DATE" 2>&1 | tee -a "$LOG"; then
    echo "[publish] FAIL: narrative row incomplete; press LLM narrative first" | tee -a "$LOG"
    exit 2
fi
echo "[publish] step 1: rerender_for_date.py --date $DATE --no-llm" | tee -a "$LOG"
./.venv/bin/python rerender_for_date.py --date "$DATE" --no-llm 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
if [ "$RC" -ne 0 ]; then
  echo "[publish] FAIL at rerender rc=$RC" | tee -a "$LOG"
  exit $RC
fi

# Step 2: copy to brief_today.html + index.html
cp -f web/archive/$DATE.html web/brief_today.html
cp -f web/archive/$DATE.html web/index.html
echo "[publish] step 2: copied to web/brief_today.html + web/index.html" | tee -a "$LOG"

# Step 2.5: rebuild archive/manifest.json from disk.
# archive_and_publish.sh is the path /pult and manual rerenders use; without
# this step the "Все брифы" list rendered on every page silently falls behind
# the archive (this was the bug for 2026-07-11/12).
if ! ./.venv/bin/python scripts/sync_archive_manifest.py 2>&1 | tee -a "$LOG"; then
    echo "[publish] FAIL at sync_archive_manifest.py" | tee -a "$LOG"
    exit 1
fi

# Step 3: git add + commit + push
if [ "$SKIP_PUSH" = "1" ]; then
  echo "[publish] step 3: SKIPPED (--skip-push)" | tee -a "$LOG"
  echo "[publish] done local-only $(date -u +%FT%TZ)" | tee -a "$LOG"
  exit 0
fi

git add web/archive/$DATE.html web/archive/manifest.json web/brief_today.html web/index.html
if git diff --cached --quiet; then
  echo "[publish] nothing to commit" | tee -a "$LOG"
  exit 0
fi
git -c user.email=hermes@developer -c user.name=Hermes \
  commit -m "Publish brief $DATE" 2>&1 | tee -a "$LOG"
git push origin main 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
if [ "$RC" -ne 0 ]; then
  echo "[publish] FAIL at git push rc=$RC" | tee -a "$LOG"
  exit $RC
fi

echo "[publish] done rc=0 $(date -u +%FT%TZ)" | tee -a "$LOG"
exit 0