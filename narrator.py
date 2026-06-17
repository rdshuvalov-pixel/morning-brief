"""Narrator — generates brief narrative + design fields via LLM (MiniMax M2.7) or fallback."""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from models import BriefContext, DayStatus

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "Ты пишешь структурированные утренние брифы о здоровье на русском языке."
NARRATIVE_PROMPT = """Ты умный друг, который каждое утро анализирует здоровье и готовит бриф.

Данные за {date}:
- Сон: {sleep_score} баллов, {sleep_duration} часов
- HRV: {hrv}, RHR: {rhr}
- Body Battery: {body_battery}, Recovery: {recovery}
- SpO2: {spo2}%
- Stress: {stress}
- Helio Readiness: {readiness}, Physical: {physical}
- Еда: {food_summary}
- Статус: {status}

Напиши 2-3 связных абзаца на русском. Тон — дружелюбный умный друг. Упомяни конкретные цифры. Дай 1-2 совета."""


TITLE_PROMPT = """По данным за {date} (статус {status}):
- Sleep Score {sleep_score}, HRV {hrv}, RHR {rhr}
- Body Battery {body_battery}, Recovery {recovery}
- Readiness {readiness}, Stress {stress}

Придумай ОДНУ короткую фразу (3-8 слов) для заголовка брифа. Тон — тёплый, дружелюбный. Только фразу, без кавычек и пояснений."""


SUMMARY_PROMPT = """Данные: Sleep {sleep_score}, HRV {hrv}, RHR {rhr}, Body Battery {body_battery}, Readiness {readiness}.

Напиши ОДНО предложение (до 20 слов) краткой сводки под заголовок. Тон — тёплый, позитивный. Без кавычек."""


DAILY_TITLE_PROMPT = """Статус дня: {status}. Sleep Score {sleep_score}, Recovery {recovery}, Readiness {readiness}.

Придумай короткий заголовок для блока "Общая сводка" (3-10 слов). Тон — дружелюбный совет. Только заголовок, без кавычек."""


DAILY_TEXT_PROMPT = """Статус: {status}. Body Battery {body_battery}, Readiness {readiness}, Stress {stress}, HRV {hrv}.
Календарь: {meetings_count} встреч.
Еда: {food_summary}.

Напиши 1-2 предложения (до 40 слов) совета на день. Тон — тёплый умный друг. Без кавычек."""


def generate(ctx: BriefContext, status: DayStatus) -> tuple[
    str,           # narrative
    str,           # headline
    str,           # headline_summary
    str,           # daily_summary_title
    str,           # daily_summary_text
    Literal["llm", "fallback"],
]:
    try:
        return _llm_all(ctx, status)
    except Exception as e:
        logger.warning("LLM failed, using fallback: %s", e)
        return _fallback(ctx, status)


def _g(val, default="—"):
    return str(val) if val is not None else default


def _build_common(ctx: BriefContext, status: DayStatus) -> dict:
    g = ctx.garmin
    h = ctx.helio
    sleep_h = f"{round(g.sleep_duration_min / 60, 1)}" if g and g.sleep_duration_min else "—"
    food_cal = sum(e.kcal for e in ctx.food)
    meetings = len(ctx.calendar) if ctx.calendar else 0

    return {
        "date":    ctx.date.isoformat(),
        "status":  status.status.upper(),
        "sleep_score":   _g(g.sleep_score if g else None),
        "sleep_duration": sleep_h,
        "hrv":     _g(g.hrv if g else (h.hrv_score if h else None)),
        "rhr":     _g(g.rhr if g else (h.rhr if h else None)),
        "body_battery": _g(g.body_battery if g else None),
        "recovery": _g(g.training_readiness if g else None),
        "spo2":    _g(g.spo2 if g else None),
        "stress":  _g(g.stress if g else None),
        "readiness": _g(h.readiness if h else None),
        "physical": _g(h.physical if h else None),
        "food_summary": f"{food_cal} ккал" if ctx.food else "нет данных",
        "meetings_count": meetings,
    }


def _llm_all(ctx: BriefContext, status: DayStatus) -> tuple:
    common = _build_common(ctx, status)

    try:
        narrative = _llm_call(NARRATIVE_PROMPT.format(**common), max_tokens=500)
    except Exception:
        narrative = _fallback_narrative(ctx, status)

    try:
        headline = _llm_call(TITLE_PROMPT.format(**common), max_tokens=30).strip()
    except Exception:
        headline = "Доброе утро!"

    try:
        headline_summary = _llm_call(SUMMARY_PROMPT.format(**common), max_tokens=40).strip()
    except Exception:
        headline_summary = ""

    try:
        daily_title = _llm_call(DAILY_TITLE_PROMPT.format(**common), max_tokens=40).strip()
    except Exception:
        daily_title = "Главный совет на сегодня"

    try:
        daily_text = _llm_call(DAILY_TEXT_PROMPT.format(**common), max_tokens=80).strip()
    except Exception:
        daily_text = ""

    return narrative, headline, headline_summary, daily_title, daily_text, "llm"


def _llm_call(prompt: str, max_tokens: int = 300) -> str:
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    with httpx.AsyncClient(timeout=30) as client:
        resp = client.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        raw = resp.json()
    return raw["choices"][0]["message"]["content"].strip()


def _fallback_narrative(ctx: BriefContext, status: DayStatus) -> str:
    g = ctx.garmin
    h = ctx.helio
    parts = []

    if g:
        parts.append(
            f"Сон: {g.sleep_score or '—'} баллов, "
            f"{round((g.sleep_duration_min or 0)/60, 1)}ч, "
            f"HRV {g.hrv or '—'}, RHR {g.rhr or '—'}."
        )
    if h:
        parts.append(
            f"Readiness {h.readiness or '—'}%, "
            f"Physical {h.physical or '—'}%."
        )
    if ctx.food:
        parts.append(f"Еда: {sum(e.kcal for e in ctx.food)} ккал.")

    return " ".join(parts) if parts else "Нет данных."


def _fallback(ctx: BriefContext, status: DayStatus) -> tuple:
    narrative = _fallback_narrative(ctx, status)
    return (
        narrative,
        "Доброе утро!",
        "",
        "Главный совет на сегодня",
        "",
        "fallback",
    )
