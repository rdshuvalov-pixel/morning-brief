#!/usr/bin/env python3
"""Re-render the morning brief HTML for an arbitrary archive date.

Reads narrative + 5 opinions FROM DB (briefs.narrative JSON) — never calls LLM.
The HTML h1 / lead / footer / per-block opinions all come from whatever
generate_llm.py last wrote to briefs.narrative. If briefs.narrative is
missing/empty, we use the static fallback text and skip opinions.

This script is a PUBLISHER, not a generator. Generation lives in
scripts/manual/generate_llm.py and is run BEFORE this script.

Use case: archive/<date>.html was snapshotted early or the user wants
the archive file to reflect the latest narrative that lives in DB.

Usage:
    ./.venv/bin/python rerender_for_date.py --date 2026-06-29
"""
from __future__ import annotations

import argparse
import json
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

# Fallback narrative — only used when briefs.narrative is empty or malformed
# (i.e. generate_llm.py was never run for this date, OR DB row missing).
DEFAULT_HEADLINE = "Утренний бриф"
DEFAULT_LEAD = "Числовые поля собраны из БД. Нарратив-NLG не записан для этой даты."
DEFAULT_FOOTER_TITLE = "Хорошее начало"
DEFAULT_FOOTER_TEXT = "Проверь ресурс утром и не отдавай сильное утро мелочам."


def _ensure_narrative(ctx_in: dict, target: date) -> None:
    """If ctx has no narrative_* fields, fall back to defaults (no LLM call).

    fetch_live_context() already populates narrative_headline / lead / footer_*
    and opinion_* from briefs.narrative JSON when that row exists. This fn is
    only a safety net for the (rare) case of empty DB row.
    """
    if ctx_in.get("narrative_headline") or ctx_in.get("narrative_summary"):
        return
    log.warning("[%s] briefs.narrative empty in DB — using static defaults", target)
    ctx_in["narrative_headline"]    = DEFAULT_HEADLINE
    ctx_in["narrative_summary"]     = DEFAULT_LEAD
    ctx_in["narrative_footer_title"] = DEFAULT_FOOTER_TITLE
    ctx_in["narrative_footer_text"]  = DEFAULT_FOOTER_TEXT
    for k in ("opinion_weather", "opinion_tasks", "opinion_movement",
              "opinion_calendar", "opinion_battery"):
        ctx_in[k] = None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD, archive target date")
    p.add_argument("--no-llm", action="store_true",
                   help="Deprecated. Kept for compatibility — now a no-op "
                        "because this script never calls LLM anyway.")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    t0 = time.monotonic()
    log.info("[%s] fetching context from DB", target)
    ctx_in = fetch_live_context(target)
    _ensure_narrative(ctx_in, target)

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
