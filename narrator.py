"""Narrator — generates brief narrative via LLM (MiniMax M2.7) or fallback."""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from models import BriefContext, DayStatus

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """Ты умный друг, который каждое утро анализирует здоровье и показатели за вчерашний день.

Вот данные за {date}:
- Сон: {sleep_score} баллов, {sleep_duration} часов, глубокий сон {deep_sleep_pct}%
- HRV: {hrv}, RHR: {rhr}
- Body Battery: {body_battery}, Recovery Score: {training_readiness}
- SpO2: {spo2}%, Stress: {stress}
- Helio: Readiness {helio_readiness}, Physical {helio_physical}, Mental {helio_mental}
- Еда: {food_summary}
- Задачи на сегодня: {tasks_count} шт
- Status: {status_emoji} {status}

Напиши 3-4 связных абзаца на русском языке. Тон — дружелюбный умный друг. Упомяни конкретные цифры. Дай 1-2 совета что сделать сегодня."""


def generate(ctx: BriefContext, status: DayStatus) -> tuple[str, Literal["llm", "fallback"]]:
    try:
        return _llm_generate(ctx, status), "llm"
    except Exception as e:
        logger.warning("LLM failed, using fallback: %s", e)
        return _fallback(ctx, status), "fallback"


def _build_prompt(ctx: BriefContext, status: DayStatus) -> str:
    g = ctx.garmin
    h = ctx.helio

    def _v(val, default="—"):
        return str(val) if val is not None else default

    sleep_h = f"{round(g.sleep_duration_min / 60, 1)}" if g and g.sleep_duration_min else "—"
    deep = f"{g.deep_sleep_pct}" if g and g.deep_sleep_pct else "—"
    food_cal = sum(e.kcal for e in ctx.food)
    status_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}[status.status]

    return PROMPT_TEMPLATE.format(
        date=ctx.date.isoformat(),
        sleep_score=_v(g.sleep_score if g else None),
        sleep_duration=sleep_h,
        deep_sleep_pct=deep,
        hrv=_v(g.hrv if g else (h.hrv_score if h else None)),
        rhr=_v(g.rhr if g else (h.rhr if h else None)),
        body_battery=_v(g.body_battery if g else None),
        training_readiness=_v(g.training_readiness if g else None),
        spo2=_v(g.spo2 if g else None, "—"),
        stress=_v(g.stress if g else None, "—"),
        helio_readiness=_v(h.readiness if h else None),
        helio_physical=_v(h.physical if h else None),
        helio_mental=_v(h.mental if h else None),
        food_summary=f"{food_cal} ккал" if ctx.food else "нет данных",
        tasks_count=len(ctx.tasks),
        status_emoji=status_emoji,
        status=status.status.upper(),
    )


def _llm_generate(ctx: BriefContext, status: DayStatus) -> str:
    prompt = _build_prompt(ctx, status)
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Ты пишешь краткие дружелюбные утренние брифы о здоровье на русском. 3-4 абзаца."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.7,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        raw = resp.json()
    return raw["choices"][0]["message"]["content"].strip()


def _fallback(ctx: BriefContext, status: DayStatus) -> str:
    g = ctx.garmin
    h = ctx.helio
    status_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}[status.status]

    lines = [
        f"{status_emoji} Утренний бриф за {ctx.date.isoformat()} — {status.status.upper()}",
        "",
    ]
    if g:
        lines.append(f"Сон: {g.sleep_score or '—'} баллов, {round((g.sleep_duration_min or 0) / 60, 1)}ч, HRV {g.hrv or '—'}, RHR {g.rhr or '—'}")
        lines.append(f"Body Battery {g.body_battery or '—'}, Recovery {g.training_readiness or '—'}, SpO2 {g.spo2 or '—'}%")
    if h:
        lines.append(f"Helio: Readiness {h.readiness or '—'}, Physical {h.physical or '—'}, Mental {h.mental or '—'}")
    if ctx.food:
        total_kcal = sum(e.kcal for e in ctx.food)
        lines.append(f"Еда: {total_kcal} ккал ({len(ctx.food)} приёма)")
    if status.reasons:
        lines.append("")
        lines.extend(status.reasons)
    return "\n".join(lines)
