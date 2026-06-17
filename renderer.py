"""Renderer — HTML (Jinja2) and Telegram text rendering."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from models import BriefContext, DayStatus

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
)


def _status_color(status: DayStatus) -> str:
    return {
        "green": "#22c55e",
        "yellow": "#eab308",
        "red": "#ef4444",
        "grey": "#6b7280",
    }.get(status.status, "#6b7280")


def _status_emoji(status: DayStatus) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}.get(status.status, "⚪")


def render_html(narrative: str, ctx: BriefContext, status: DayStatus) -> str:
    template = _env.get_template("brief.html.j2")

    total_kcal    = sum(e.kcal for e in ctx.food) if ctx.food else 0
    total_protein = sum(e.protein for e in ctx.food) if ctx.food else 0
    total_fat     = sum(e.fat for e in ctx.food) if ctx.food else 0
    total_carbs   = sum(e.carbs for e in ctx.food) if ctx.food else 0

    return template.render(
        date=ctx.date.isoformat(),
        status=status,
        status_color=_status_color(status),
        status_emoji=_status_emoji(status),
        narrative=narrative,
        garmin=ctx.garmin,
        helio=ctx.helio,
        food=ctx.food,
        total_kcal=total_kcal,
        total_protein=total_protein,
        total_fat=total_fat,
        total_carbs=total_carbs,
        weather=ctx.weather,
        calendar=ctx.calendar,
        tasks=ctx.tasks,
    )


def render_telegram(narrative: str, status: DayStatus, brief_url: str | None = None) -> str:
    emoji = _status_emoji(status)
    lines = [f"{emoji} *Утренний бриф — {status.status.upper()}*", "", narrative]
    if brief_url:
        lines.append(f"\n🌐 {brief_url}")
    return "\n".join(lines)
