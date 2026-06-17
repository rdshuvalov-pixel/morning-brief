"""Renderer — HTML (Jinja2) and Telegram text rendering."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from models import BriefContext, DayStatus

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
)


def _status_color(status: DayStatus) -> str:
    return {
        "green": "#2d9b7d",
        "yellow": "#d38a2c",
        "red": "#f06757",
        "grey": "#6d617f",
    }.get(status.status, "#6d617f")


def _status_emoji(status: DayStatus) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}.get(status.status, "⚪")


def _hrv_status(garmin, helio) -> str:
    hrv = (garmin.hrv if garmin and garmin.hrv
           else helio.hrv_score if helio and helio.hrv_score else None)
    if hrv is None:
        return "—"
    if hrv >= 55:
        return "Выше базы"
    if hrv >= 40:
        return "В норме"
    return "Ниже базы"


def _build_agenda(calendar, tasks) -> list[dict]:
    items = []
    for cal in (calendar or []):
        items.append({
            "title":       cal.title,
            "description": None,
            "time_primary": cal.start_time,
            "time_secondary": (str(cal.duration_minutes) + " мин") if cal.duration_minutes else None,
        })
    for task in (tasks or []):
        items.append({
            "title":       task.title,
            "description": None,
            "time_primary": f"p{task.priority}",
            "time_secondary": None,
        })
    return items


def _activity_label(balance: int) -> str:
    if balance < -300:
        return "Съедено больше нормы"
    if balance > 300:
        return "Активный день"
    return "В балансе"


def _focus_window(status: DayStatus, calendar) -> str:
    if not calendar:
        return "Свободное утро"
    morning = [e for e in calendar if e.start_time and e.start_time < "12:00"]
    if morning:
        first = min(morning, key=lambda e: e.start_time or "23:59")
        return f"Focus {first.start_time or 'утро'}"
    return "Meetings с утра"


def render_html(
    narrative: str,
    ctx: BriefContext,
    status: DayStatus,
    *,
    headline: str | None = None,
    headline_summary: str | None = None,
    daily_summary_title: str | None = None,
    daily_summary_text: str | None = None,
    body_battery_delta: int | None = None,
) -> str:
    template = _env.get_template("brief.html.j2")

    total_kcal    = sum(e.kcal for e in ctx.food) if ctx.food else 0
    kcal_balance  = (ctx.helio.kcal if ctx.helio and ctx.helio.kcal else 0) - total_kcal

    agenda = _build_agenda(ctx.calendar, ctx.tasks)

    return template.render(
        # Standard
        date=ctx.date.isoformat(),
        status=status,
        narrative=narrative,
        garmin=ctx.garmin,
        helio=ctx.helio,
        food=ctx.food,
        weather=ctx.weather,
        calendar=ctx.calendar,
        tasks=ctx.tasks,
        # New design fields
        headline=headline,
        headline_summary=headline_summary,
        sleep_block_status="Night Stable" if status.status == "green" else ("Night Interrupted" if status.status == "yellow" else "Recovery Night"),
        hrv_status_text=_hrv_status(ctx.garmin, ctx.helio),
        spo2_status_text="SpO2 " + str(ctx.garmin.spo2) + "%" if ctx.garmin and ctx.garmin.spo2 else "SpO2 —",
        body_battery_delta=body_battery_delta,
        focus_window_label=_focus_window(status, ctx.calendar),
        readiness_hint=str(ctx.helio.readiness) + "%" if ctx.helio and ctx.helio.readiness is not None else None,
        stress_hint="Стресс " + str(ctx.garmin.stress) if ctx.garmin and ctx.garmin.stress is not None else None,
        spo2_hint="Ровное дыхание" if ctx.garmin and ctx.garmin.spo2 and ctx.garmin.spo2 >= 95 else "Проветрить",
        agenda_items=agenda,
        activity_status_label=_activity_label(kcal_balance),
        steps_goal=10000,
        activity_summary_text=None,
        daily_summary_title=daily_summary_title,
        daily_summary_text=daily_summary_text,
    )


def render_telegram(narrative: str, status: DayStatus, brief_url: str | None = None) -> str:
    emoji = _status_emoji(status)
    lines = [f"{emoji} *Утренний бриф — {status.status.upper()}*", "", narrative]
    if brief_url:
        lines.append(f"\n🌐 {brief_url}")
    return "\n".join(lines)
