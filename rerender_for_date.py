#!/usr/bin/env python3
"""Re-render the morning brief HTML for an arbitrary date from DB ONLY.

Use case: archive/<date>.html was snapshotted at 06:02 on <date>, before
the 06:30 Garmin cron populated garmin_metrics for that date. The shipped
archive therefore shows empty Garmin fields. This script re-renders against
the CURRENT DB state (which has the data) and writes the result to
web/archive/<date>.html, leaving today's brief_today.html untouched.

Skips the slow narrative-LLM path (compose/compose_all_opinions) by
monkey-patching them with no-ops before fetch_live_context calls them.

Usage:
    set -a && . ./.env && set +a && \
        ./venv/bin/python rerender_for_date.py --date 2026-06-29
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/root/morning_brief_v2")

# Load env BEFORE any supabase import reads it
for _line in Path("/root/morning_brief_v2/.env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#"):
        continue
    _k, _, _v = _line.partition("=")
    import os
    os.environ.setdefault(_k, _v)

# ── Patch narrative LLM calls to no-ops BEFORE import. ──────────────────
# compose is sync; compose_all_opinions is async.
# Both are imported lazily inside fetch_live_context, so we must patch
# after the import-time side-effect (which is just defining the module)
# but BEFORE fetch_live_context runs.
import playful.narrative as _n  # noqa: E402


def _no_compose(_facts):
    return None


async def _no_opinions(_facts):
    return {
        "weather": None,
        "tasks": None,
        "movement": None,
        "calendar": None,
        "battery": None,
    }


_n.compose = _no_compose
_n.compose_all_opinions = _no_opinions

from playful.render_playful import (  # noqa: E402
    build_playful_context,
    fetch_live_context,
    render_playful_html,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rerender_for_date")

ROOT = Path("/root/morning_brief_v2")
ARCHIVE = ROOT / "web" / "archive"

DEFAULT_HEADLINE = "Архив восстановлен"
DEFAULT_LEAD = "Снимок перерисован с актуальными числовыми полями из БД."
DEFAULT_FOOTER_TITLE = "Архив"
DEFAULT_FOOTER_TEXT = "Архив восстановлен после позднего прибытия данных Garmin."


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD, archive target date")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    log.info("[%s] fetching context from DB", target)
    ctx_in = fetch_live_context(target)

    # Override narrative with static fallback
    ctx_in["narrative_headline"] = DEFAULT_HEADLINE
    ctx_in["narrative_summary"] = DEFAULT_LEAD
    ctx_in["narrative_footer_title"] = DEFAULT_FOOTER_TITLE
    ctx_in["narrative_footer_text"] = DEFAULT_FOOTER_TEXT

    log.info("[%s] building playful context", target)
    ctx = build_playful_context(**ctx_in)

    log.info("[%s] rendering HTML", target)
    html = render_playful_html(ctx)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE / f"{target.isoformat()}.html"
    out.write_text(html, encoding="utf-8")
    log.info("[%s] wrote %s (%d bytes)", target, out, len(html))
    return 0


if __name__ == "__main__":
    sys.exit(main())
