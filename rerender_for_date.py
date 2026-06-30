#!/usr/bin/env python3
"""Re-render the morning brief HTML for an arbitrary archive date.

By default runs the FULL pipeline (Hermes narrative + 5 block opinions)
just like the daily render. This produces real text in <h1>, <p class="lead">,
the tasks footer, and 5 opinion lines under each card — not technical
fallback text.

Use case: archive/<date>.html was snapshotted early (e.g. before 06:30 Garmin
cron populated garmin_metrics for that date). This script re-reads the
CURRENT DB rows and rebuilds the rendered HTML against them.

Options:
    --no-llm       Skip narrative + opinions, use static fallback text.
                   Faster (~0.5s) but you get "Архив восстановлен" h1 —
                   use only as last resort.

Usage:
    set -a && . ./.env && set +a && \
        ./venv/bin/python rerender_for_date.py --date 2026-06-29
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/root/morning_brief_v2")

# Load .env BEFORE supabase reads it
for _line in Path("/root/morning_brief_v2/.env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#"):
        continue
    _k, _, _v = _line.partition("=")
    import os
    os.environ.setdefault(_k, _v)

from playful.render_playful import (  # noqa: E402
    build_playful_context,
    fetch_live_context,
    render_playful_html,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rerender_for_date")

ROOT = Path("/root/morning_brief_v2")
ARCHIVE = ROOT / "web" / "archive"

# Fallback narrative (used only with --no-llm)
DEFAULT_HEADLINE = "Архив восстановлен"
DEFAULT_LEAD = "Снимок перерисован с актуальными числовыми полями из БД."
DEFAULT_FOOTER_TITLE = "Архив"
DEFAULT_FOOTER_TEXT = "Числовые поля восстановлены из БД."


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD, archive target date")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip narrative-LLM (faster, but text becomes static fallback)")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    t0 = time.monotonic()
    log.info("[%s] fetching context from DB", target)
    ctx_in = fetch_live_context(target)

    if args.no_llm:
        log.info("[%s] --no-llm flag set, overriding narrative with static fallback", target)
        ctx_in["narrative_headline"] = DEFAULT_HEADLINE
        ctx_in["narrative_summary"] = DEFAULT_LEAD
        ctx_in["narrative_footer_title"] = DEFAULT_FOOTER_TITLE
        ctx_in["narrative_footer_text"] = DEFAULT_FOOTER_TEXT
        for k in ("opinion_weather", "opinion_tasks", "opinion_movement",
                  "opinion_calendar", "opinion_battery"):
            ctx_in[k] = None

    log.info("[%s] building playful context (%.1fs elapsed)", target, time.monotonic() - t0)
    ctx = build_playful_context(**ctx_in)

    log.info("[%s] rendering HTML (%.1fs elapsed)", target, time.monotonic() - t0)
    html = render_playful_html(ctx)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE / f"{target.isoformat()}.html"
    out.write_text(html, encoding="utf-8")

    h1_match = ""
    import re
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m:
        h1_match = f", h1={m.group(1).strip()[:50]!r}"
    log.info(
        "[%s] wrote %s (%d bytes, %.1fs wall-clock%s)",
        target, out, len(html), time.monotonic() - t0, h1_match,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
