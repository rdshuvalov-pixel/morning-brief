#!/usr/bin/env python3
"""DEPRECATED wrapper: redirects to run_garmin.py with --date yesterday.

Use run_garmin.py directly. This file kept only for back-compat with any
existing cron entries that still call it.
"""
from __future__ import annotations
import subprocess
import sys
from datetime import date, timedelta

if __name__ == "__main__":
    y = (date.today() - timedelta(days=1)).isoformat()
    print(f"[deprecated] forwarding to run_garmin.py --date {y}", file=sys.stderr)
    rc = subprocess.call([sys.executable, "/root/morning_brief_v2/run_garmin.py", "--date", y])
    sys.exit(rc)