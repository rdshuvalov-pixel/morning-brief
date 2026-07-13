#!/usr/bin/env python3
"""Rebuild web/archive/manifest.json from files actually on disk.

Single source of truth for the "Все брифы" list rendered by every HTML page.
Called by both archive_brief_cron.sh (07:30 Lisbon) and archive_and_publish.sh
(manual / /pult publish path) so the list never falls behind again.

Idempotent: re-runs without changes still produce identical JSON, but the
caller should `git diff --cached --quiet` before committing.

Exits 0 on success, 1 if web/archive/ is missing.
"""
import json
import sys
from pathlib import Path

ROOT = Path("/root/morning_brief_v2")
ARCHIVE = ROOT / "web" / "archive"
MANIFEST = ARCHIVE / "manifest.json"


def main() -> int:
    if not ARCHIVE.is_dir():
        print(f"sync_archive_manifest: missing {ARCHIVE}", file=sys.stderr)
        return 1
    dates = sorted([p.stem for p in ARCHIVE.glob("*.html")], reverse=True)
    # `updated` = newest date on disk (not wall clock — caller may pass an
    # explicit date for rerender; what's listed is what's published).
    updated = dates[0] if dates else ""
    MANIFEST.write_text(
        json.dumps({"dates": dates, "updated": updated}, indent=2, ensure_ascii=False)
        + "\n"
    )
    print(f"sync_archive_manifest: {len(dates)} dates, updated={updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())