"""Save today's brief HTML snapshot into web/archive/ for git history.

Usage:
    python archive_brief.py                  # uses today's Lisbon date
    python archive_brief.py --date 2026-06-28 # explicit date (cron uses this)

Idempotent: re-runs without changes produce no diff in git.
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/morning_brief_v2")
SRC = ROOT / "web" / "brief_today.html"
ARCHIVE = ROOT / "web" / "archive"
ARCHIVE.mkdir(parents=True, exist_ok=True)
MANIFEST = ARCHIVE / "manifest.json"


def lisbon_date_str(explicit: str | None) -> str:
    if explicit:
        return explicit
    # Lisbon date — matches /etc/cron.d TZ
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Lisbon")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: today in Europe/Lisbon)")
    args = p.parse_args()

    date_str = lisbon_date_str(args.date)
    dst = ARCHIVE / f"{date_str}.html"

    if not SRC.exists():
        print(f"archive_brief: src missing {SRC.relative_to(ROOT)}", file=sys.stderr)
        return 1

    src_bytes = SRC.read_bytes()
    src_hash = hashlib.sha256(src_bytes).hexdigest()[:16]

    if dst.exists():
        dst_hash = hashlib.sha256(dst.read_bytes()).hexdigest()[:16]
        if src_hash == dst_hash:
            print(f"archive_brief: {date_str}.html unchanged (hash={src_hash}), skip copy")
        else:
            shutil.copy2(SRC, dst)
            print(f"archive_brief: {date_str}.html updated (old={dst_hash} new={src_hash})")
    else:
        shutil.copy2(SRC, dst)
        print(f"archive_brief: copied → {dst.relative_to(ROOT)} (hash={src_hash})")

    # Update manifest.json — list of dates, newest first
    dates = sorted([p.stem for p in ARCHIVE.glob("*.html")], reverse=True)
    MANIFEST.write_text(
        json.dumps({"dates": dates, "updated": date_str}, indent=2, ensure_ascii=False)
    )
    print(f"archive_brief: manifest.json → {len(dates)} dates")
    return 0


if __name__ == "__main__":
    sys.exit(main())