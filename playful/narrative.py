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