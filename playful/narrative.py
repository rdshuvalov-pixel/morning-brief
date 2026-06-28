"""LLM-narrative for the morning brief.

Generates 4 short text fields for the brief HTML (headline, lead, footer_title,
footer_text) from today's metrics. Voice: мотивационный/действенный, без
армейской лексики (никаких «рядовой», «солдат», «казарма», «в строй»).

Pipeline:
  metrics dict -> system+user prompt -> hermes -z (one-shot) -> parse JSON -> dict

Failure modes (any of these returns None and caller falls back to defaults):
  - hermes binary not in PATH
  - subprocess timeout (default 45s)
  - non-zero exit
  - output doesn't parse as JSON
  - missing required keys
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты пишешь мотивационные тексты для утреннего брифа.
Тон: дерзкий, прямой, короткие рубленые фразы. Никакой армейской лексики —
никаких «рядовой», «солдат», «сержант», «казарма», «в строй», «подъём».
Мотивация без пафоса. Конкретика из цифр, не абстракции.

Формат ответа СТРОГО — JSON с 4 полями:
{
  "headline": "3-6 слов, провокация. Без точки.",
  "lead": "1-2 предложения. Метрики + интерпретация.",
  "footer_title": "3-5 слов. Закрытая мысль.",
  "footer_text": "1-3 предложения. Actionable сводка."
}

Никаких префиксов вроде "Вот JSON:" — только валидный JSON.
"""


FEW_SHOT = """Примеры эталона тона (НЕ копируй дословно — это ориентир):

headline: "97 — и это только разминка"
lead: "97/100 — заряжен. Сон был так себе (Night Uneven, score 77), но пульс 46 и HRV 70 говорят мне одно — ты в порядке. Погода ясная. Не трать день на ерунду."
footer_title: "День открыт. Не растрать"
footer_text: "97 батарейка и −51 ккал баланс — это не оправдание для лёгкого утра. Возьми задачу уровня p3 и не отпускай, пока не сделаешь."

headline: "Тело готово. Голова — посмотрим"
lead: "5ч 58м сна, батарейка 97. Это не логика — это ты. HRV 70, стресс 11. Тело готово делать вещи, которые вчера казались невозможными."
footer_title: "Закрывай утро — потом поздно"
footer_text: "Топ-задача — «Изучить dikidi…». Погода ясная, батарейка полная. Если к обеду не сдвинешь — вечером будешь разговаривать сам с собой. Я серьёзно."
"""


def _format_user_prompt(facts: dict[str, Any]) -> str:
    """Build a compact user prompt with today's metrics as facts."""
    lines = [f"Дата брифа: {facts.get('brief_date', 'unknown')}."]
    lines.append("")
    lines.append("Метрики (используй, но не перечисляй все подряд — выбери 2-3 ярких):")

    # Battery
    bb = facts.get("body_battery")
    bb_delta = facts.get("body_battery_delta")
    if bb is not None:
        bb_str = f"Body Battery: {bb}/100"
        if bb_delta is not None:
            sign = "+" if bb_delta >= 0 else ""
            bb_str += f" (vs вчера {sign}{bb_delta})"
        lines.append(f"- {bb_str}")

    # Sleep
    sleep_label = facts.get("sleep_label")
    sleep_score = facts.get("sleep_score")
    sleep_pill = facts.get("sleep_pill")
    if sleep_label:
        s = f"Сон: {sleep_label}"
        if sleep_score is not None:
            s += f", score {sleep_score}"
        if sleep_pill:
            s += f" ({sleep_pill})"
        lines.append(f"- {s}")

    # HRV/RHR
    hrv = facts.get("hrv")
    rhr = facts.get("rhr")
    if hrv or rhr:
        parts = []
        if hrv: parts.append(f"HRV {hrv}")
        if rhr: parts.append(f"пульс {rhr}")
        lines.append(f"- {' / '.join(parts)}")

    # Stress / SpO2
    stress = facts.get("stress")
    spo2 = facts.get("spo2")
    if stress is not None or spo2 is not None:
        parts = []
        if stress is not None: parts.append(f"стресс {stress}")
        if spo2 is not None: parts.append(f"SpO2 {spo2}")
        lines.append(f"- {' / '.join(parts)}")

    # Movement (yesterday)
    steps = facts.get("steps_yesterday")
    balance = facts.get("balance")
    if steps or balance is not None:
        parts = []
        if steps: parts.append(f"шаги вчера {steps:,}".replace(",", " "))
        if balance is not None:
            sign = "+" if balance >= 0 else ""
            parts.append(f"баланс {sign}{balance} ккал")
        lines.append(f"- {' / '.join(parts)}")

    # Weather
    weather = facts.get("weather_summary")
    if weather:
        lines.append(f"- Погода: {weather}")

    # Tasks
    tasks_count = facts.get("tasks_count")
    top_task = facts.get("top_task")
    if tasks_count:
        s = f"Задач: {tasks_count}"
        if top_task:
            s += f", топ: «{top_task}»"
        lines.append(f"- {s}")

    lines.append("")
    lines.append("Сгенерируй JSON с 4 полями (headline, lead, footer_title, footer_text).")

    return "\n".join(lines)


def compose(facts: dict[str, Any], *, timeout: int = 45) -> dict[str, str] | None:
    """Generate narrative via Hermes gateway. Returns None on any failure.

    Args:
        facts: dict of metrics (see _format_user_prompt for keys).
        timeout: subprocess timeout in seconds.

    Returns:
        {headline, lead, footer_title, footer_text} or None.
    """
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        logger.warning("narrative: 'hermes' binary not in PATH, skipping LLM call")
        return None

    user_prompt = _format_user_prompt(facts)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{FEW_SHOT}\n\n{user_prompt}"

    try:
        proc = subprocess.run(
            [hermes_bin, "-z", full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("narrative: hermes -z timed out after %ds", timeout)
        return None
    except Exception as e:
        logger.warning("narrative: subprocess failed: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning("narrative: hermes exit %d, stderr=%s", proc.returncode, proc.stderr[:200])
        return None

    raw = proc.stdout.strip()
    if not raw:
        logger.warning("narrative: hermes returned empty stdout")
        return None

    # Strip markdown code fence if model wraps JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Drop first ``` line and last ``` line
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("narrative: JSON parse failed: %s; raw=%r", e, raw[:300])
        return None

    required = {"headline", "lead", "footer_title", "footer_text"}
    if not isinstance(data, dict) or not required.issubset(data):
        logger.warning("narrative: missing keys %s in %s", required - set(data or {}), list(data or {}))
        return None

    # Trim each field — defensive against LLM over-generation
    out = {k: str(data[k]).strip() for k in required}
    return out


# ────────────────────────────────────────────────────────────────────────────
# Block opinions — short 1-sentence commentary per card.
# Async because we fire 4 hermes calls in parallel via asyncio.gather.
# Each one falls back to None independently; missing blocks are skipped in
# the template ({% if block_opinion %}{% endif %}).
# ────────────────────────────────────────────────────────────────────────────

OPINION_BLOCKS = ("weather", "tasks", "movement", "calendar", "battery")


async def _compose_block_async(block_name: str, facts: dict[str, Any], *, timeout: int = 60) -> str | None:
    """Fire one hermes -z call, return opinion string or None on failure.

    Per-block prompt is built from a small template + facts dict; system
    prompt enforces tone and length constraints.
    """
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        logger.warning("narrative[%s]: hermes missing, skip", block_name)
        return None

    user_prompt = _format_opinion_prompt(block_name, facts)
    full_prompt = f"{OPINION_SYSTEM_PROMPT}\n\n{OPINION_FEWSHOT}\n\n{user_prompt}"

    proc = await asyncio.create_subprocess_exec(
        hermes_bin, "-z", full_prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("narrative[%s]: hermes timeout %ds", block_name, timeout)
        return None

    if proc.returncode != 0:
        logger.warning("narrative[%s]: exit %d, stderr=%s", block_name, proc.returncode, stderr_b[:200].decode(errors="replace"))
        return None

    raw = stdout_b.decode("utf-8", errors="replace").strip()
    if not raw:
        return None

    # Strip markdown fence if LLM wrapped JSON
    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("narrative[%s]: JSON parse failed; raw=%r", block_name, raw[:200])
        return None

    if not isinstance(data, dict) or "opinion" not in data:
        return None

    opinion = str(data["opinion"]).strip()
    # Length sanity — short snippet, max 250 chars
    if len(opinion) > 250:
        opinion = opinion[:247].rstrip() + "…"
    return opinion or None


async def compose_all_opinions(facts_by_block: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """Fire all per-block opinion calls sequentially with small jitter.

    Args:
        facts_by_block: {block_name: facts_dict} for each block in OPINION_BLOCKS.

    Returns:
        {block_name: opinion_str_or_None}. Missing/empty opinions stay None.

    Why sequential: Hermes gateway serializes backend calls anyway, and 5
    concurrent `hermes -z` processes hammer it and time out (observed
    2026-06-28: all 5 timed out at 30s). Sequential with 0.2s stagger
    completes in ~40s wall-clock and is reliable.
    """
    import asyncio as _aio
    out: dict[str, str | None] = {}
    for name in OPINION_BLOCKS:
        out[name] = await _compose_block_async(name, facts_by_block.get(name, {}))
        await _aio.sleep(0.2)  # small stagger to avoid hammering
    return out


OPINION_SYSTEM_PROMPT = """Ты пишешь короткое мнение (1 предложение, до 250 символов) про блок в утреннем брифе.
Тон: дерзкий, прямой, мотивационный. Без армейской лексики (никаких «рядовой», «солдат», «казарма»).
Говори как старший товарищ, который знает что делает — коротко и по делу.
Не повторяй числа из фактов если они уже видны в самом блоке — добавь интерпретацию или действие.

Формат ответа СТРОГО — JSON с одним полем:
{"opinion": "одно предложение, до 250 символов"}

Никаких префиксов — только валидный JSON.
"""


OPINION_FEWSHOT = """Эталоны тона (НЕ копируй дословно — это ориентир):

weather: {"opinion": "Ясно и тепло. Окна нараспашку, встречу на 10 — лучше пешком дойти."}
tasks: {"opinion": "39 задач и только две с приоритетом p3. Закрой эти две — остальные сами рассосутся."}
movement: {"opinion": "4348 шагов — не план, не антиплан. Закрывай 7к, иначе вечером будешь ныть."}
calendar: {"opinion": "Одна встреча и Deep Work — идеальный день для главного. Не разменивайся."}
battery: {"opinion": "97 батарейка после 5ч 58м сна — не логика, а ты. Используй это до обеда, потом просядет."}
"""


def _format_opinion_prompt(block_name: str, facts: dict[str, Any]) -> str:
    """Per-block user prompt with relevant facts."""
    if block_name == "weather":
        cond = facts.get("condition", "—")
        day_t = facts.get("temp_day", "—")
        return f"Погода сегодня: {cond}, днём {day_t}°. Дай одно предложение — стоит ли выходить на улицу или посидеть дома."

    if block_name == "tasks":
        n = facts.get("count", 0)
        top = facts.get("top_task") or "—"
        p3_count = facts.get("p3_count", 0)
        return (
            f"Задач на сегодня: {n}. Топ: «{top}». Задач уровня p3: {p3_count}. "
            "Дай одно предложение — на чём сфокусироваться."
        )

    if block_name == "movement":
        steps = facts.get("steps_yesterday")
        balance = facts.get("balance_yesterday")
        eaten = facts.get("kcal_eaten_yesterday")
        burned = facts.get("kcal_burned_yesterday")
        return (
            f"Вчера: шаги {steps}, съел {eaten} ккал, потратил {burned} ккал, "
            f"баланс {balance:+d} ккал. Дай одно предложение — что скорректировать сегодня."
        )

    if block_name == "calendar":
        meetings = facts.get("meetings_count", 0)
        deepwork = facts.get("deepwork_minutes", 0)
        free = facts.get("free_day", False)
        if free:
            return "Сегодня нет встреч — свободный день. Дай одно предложение — на что потратить."
        return (
            f"Встреч: {meetings}, Deep Work: {deepwork} мин. "
            "Дай одно предложение — как использовать этот день."
        )

    if block_name == "battery":
        bb = facts.get("body_battery")
        sleep_label = facts.get("sleep_label", "—")
        sleep_score = facts.get("sleep_score")
        hrv = facts.get("hrv")
        return (
            f"Body Battery: {bb}/100, сон {sleep_label} (score {sleep_score}), HRV {hrv}. "
            "Дай одно предложение — на что ставить ставку сегодня."
        )

    return f"Блок: {block_name}. Дай одно предложение."