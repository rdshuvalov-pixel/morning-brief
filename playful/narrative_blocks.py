"""Per-block LLM narrative — 2-4 предложения на каждый из 5 блоков брифа.

Развитие narrative.py:
  compose()              — общий narrative (headline/lead/footer). Один вызов LLM.
  compose_all_opinions() — 5 блоков × 1 предложение (≤250 символов). Sequential.
  compose_all_blocks()   — 5 блоков × 2-4 предложения (≤500 символов). Sequential.

Per-block narratives живут ТОЛЬКО в пяти briefs.narrative_* колонках. В Telegram
не идут — там и так тесно. Их читает браузерный HTML-рендер (Playful-версия)
и показывает под opinion'ом каждого блока.

Sequential (по аналогии с compose_all_opinions) — Hermes gateway serializes
backend calls anyway, конкурентные вызовы приводят к 504/timeout. Каждый
вызов ~15-40s на блок; 5 в ряд = ~75-200s total. Worker timeout для
батча — 240s, для одного блока — 90s.

Failure modes (любой из них → fallback-строка для этого блока, остальные блоки
не страдают):
  - hermes binary not in PATH
  - subprocess timeout
  - non-zero exit
  - empty stdout

Note (2026-07-18): we switched from JSON envelope to plain-text response.
Previously the dominant failure mode was `json.JSONDecodeError` because M3
occasionally emits invalid JSON across all 5 sequential calls (job 197 on
2026-07-18 09:54). Plain text is more robust; we still defensively strip
markdown fences in case the LLM wraps output in ```.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

# Reuse the same hermes binary resolution as playful/narrative.py so that
# changes to PATH candidates propagate uniformly across the package.
from playful.narrative import _resolve_hermes, HERMES_BIN_CANDIDATES  # noqa: F401

logger = logging.getLogger(__name__)


# ── Block registry ────────────────────────────────────────────────────────
# Must match OPINION_BLOCKS in playful/narrative.py (same 5 blocks).
NARRATIVE_BLOCKS = ("weather", "tasks", "movement", "calendar", "battery")

# Max chars per block narrative. Render clips to this in the jinja template too
# (defensive). Longer than opinions (250) but still tight — это публичная
# страница, не простыня.
MAX_BLOCK_CHARS = 500


SYSTEM_PROMPT = """Ты пишешь развёрнутый комментарий к одному блоку утреннего брифа (2-4 предложения, до 500 символов).

Тон: дерзкий, прямой, мотивационный. Без армейской лексики (никаких «рядовой», «солдат», «казарма», «в строй»).
Говори как старший товарищ, который знает что делает — конкретно и по делу.

ПРАВИЛА РАБОТЫ С ЧИСЛАМИ (КРИТИЧНО):
1. Ниже в user_prompt будет блок «ЗАФИКСИРОВАННЫЕ ЧИСЛА» — используй ТОЛЬКО эти числа.
2. Если какого-то числа нет в блоке (НЕТ ДАННЫХ) — НЕ выдумывай. Либо не упоминай это поле,
   либо честно скажи «без данных по X».
3. НИКОГДА не округляй, не меняй и не придумывай альтернативные числа.
4. Все остальные данные (задачи, погода, календарь) бери строго из разделов user_prompt, не из общих знаний.

Формат ответа: ОДИН АБЗАЦ чистого текста на русском, 2-4 предложения, до 500 символов.
БЕЗ JSON, БЕЗ кодоблоков ```, БЕЗ префиксов вроде "Вот текст:" / "Ответ:" / "JSON:".
Сразу начинай с первого предложения по делу."""


FEWSHOT = """Эталоны тона (НЕ копируй дословно — это ориентир):

weather: Утро ясное, днём до +24°, к вечеру прохладнее на 4°. Ветер слабый, окно для дел на улице — с 9 до 17, дальше поднимать не стоит. Если план был «дойти пешком до встречи» — самое время.
tasks: На сегодня 39 задач, из них 2 на p3 («Оформить пропуск в БЦ», «Подготовить ТЗ по CRM»). Всё остальное — p4–p5, оно подождёт. Начни с пропуска: мелкая, но открывает доступ к БЦ, без неё CRM-кейс не сдвинуть.
movement: Вчера 4348 шагов — меньше 7к, к балансу −287 ккал (съел 2104, потратил 2391). Сегодня окно для компенсации: до обеда дойти до 5к, вечером ещё 3к. Не геройствуй — две обычные прогулки, не спортивная сессия.
calendar: Одна встреча в обед, остальное время — твоё. Утром — Deep Work без переключений, после обеда — лёгкие задачи и движение. День для главного.
battery: 97 батарейка после 5ч 58м сна — это не логика, это ты. HRV 70, стресс 11 — тело в полном порядке. Используй это до обеда: главная задача, Deep Work, никакого шума. После 15:00 ресурс просядет, планируй откат."""


def _format_user_prompt(block_name: str, facts: dict[str, Any]) -> str:
    """Build per-block user prompt with explicit numbers section.

    Mirrors playful/narrative._format_opinion_prompt() but expands to
    2-4 sentences worth of facts (incl. comparisons and context).
    """
    lines: list[str] = [f"Блок: {block_name}. Дата брифа: {facts.get('brief_date', 'unknown')}."]
    lines.append("")

    # [1] ЗАФИКСИРОВАННЫЕ ЧИСЛА (LLM может использовать ТОЛЬКО их)
    lines.append("ЗАФИКСИРОВАННЫЕ ЧИСЛА (используй только эти; ничего не выдумывай):")

    def _line(key: str, label: str, fmt: str = "{}") -> str:
        v = facts.get(key)
        if v is None or v == "":
            return f"  - {label}: НЕТ ДАННЫХ"
        try:
            return f"  - {label}: {fmt.format(v)}"
        except Exception:
            return f"  - {label}: {v}"

    if block_name == "weather":
        # facts shape: {"morning": {...}, "day": {...}, "evening": {...}}
        for period in ("morning", "day", "evening"):
            p = (facts or {}).get(period) or {}
            temp = p.get("temp") or p.get("temp_day") or p.get(f"temp_{period}")
            cond = p.get("condition") or p.get("condition_day") or p.get(f"condition_{period}")
            wind = p.get("wind")
            lines.append(f"  - {period}: temp={temp if temp is not None else 'НЕТ ДАННЫХ'}, "
                         f"condition={cond if cond else 'НЕТ ДАННЫХ'}, "
                         f"wind={wind if wind is not None else 'НЕТ ДАННЫХ'} м/с")
        # Comparison with yesterday if available
        vs_yest = facts.get("vs_yesterday")
        if vs_yest:
            lines.append(f"  - vs вчера: {vs_yest}")

    elif block_name == "tasks":
        lines.append(_line("count", "Задач на сегодня"))
        lines.append(_line("p3_count", "Задач с приоритетом p3"))
        lines.append(_line("top_task", "Топ-задача"))
        items = facts.get("items") or []
        if items:
            lines.append("  - Список (топ-5):")
            for t in items[:5]:
                title = t.get("title") if isinstance(t, dict) else t
                pri = t.get("priority") if isinstance(t, dict) else None
                lines.append(f"      • p{pri or '?'} «{title}»")

    elif block_name == "movement":
        lines.append(_line("steps_yesterday", "Шаги вчера"))
        lines.append(_line("kcal_eaten_yesterday", "Съедено ккал вчера"))
        lines.append(_line("kcal_burned_yesterday", "Потрачено ккал вчера"))
        bal = facts.get("balance_yesterday")
        if bal is not None:
            sign = "+" if bal >= 0 else ""
            lines.append(f"  - Баланс: {sign}{bal} ккал")
        else:
            lines.append("  - Баланс: НЕТ ДАННЫХ")
        # Steps goal hint for context
        goal = facts.get("steps_goal")
        if goal:
            lines.append(f"  - Дневная цель шагов: {goal}")

    elif block_name == "calendar":
        lines.append(_line("meetings_count", "Встреч сегодня"))
        lines.append(_line("deepwork_minutes", "Deep Work минут (вставлено)"))
        if facts.get("free_day"):
            lines.append("  - Свободный день: ДА")
        # Top items
        items = facts.get("items") or []
        if items:
            lines.append("  - Список (топ-5):")
            for c in items[:5]:
                title = c.get("title") if isinstance(c, dict) else c
                tstart = c.get("start_time") if isinstance(c, dict) else None
                lines.append(f"      • {tstart or '??:??'} «{title}»")

    elif block_name == "battery":
        lines.append(_line("body_battery", "Body Battery (0-100)"))
        bb_delta = facts.get("body_battery_delta")
        if bb_delta is not None:
            sign = "+" if bb_delta >= 0 else ""
            lines.append(f"  - Body Battery vs вчера: {sign}{bb_delta}")
        else:
            lines.append("  - Body Battery vs вчера: НЕТ ДАННЫХ")
        lines.append(_line("sleep_label", "Сон (длительность)"))
        lines.append(_line("sleep_score", "Sleep Score (0-100)"))
        lines.append(_line("hrv", "HRV (мс)"))
        lines.append(_line("rhr", "Пульс покоя (уд/мин)"))
        lines.append(_line("stress", "Стресс (0-100)"))

    lines.append("")
    lines.append("Запрещено выводить новые числа из этих фактов. НЕ прогнозируй будущие значения, "
                 "калории, вес, ресурс или сроки; упоминай только зафиксированные числа выше.")
    lines.append("Ответь одним абзацем чистого текста на русском (2-4 предложения, до 500 символов). "
                 "Без JSON, без ```, без префиксов. Сразу по делу.")
    return "\n".join(lines)


# ── Deterministic fallback per block (when LLM fails) ────────────────────
# Pitfall §27 — если LLM упал, не оставлять блок пустым. Используем факты
# из ctx, чтобы render не висел с дыркой.
def _fallback_block_text(block_name: str, facts: dict[str, Any]) -> str:
    """Return a short deterministic 2-4 sentence summary without LLM.

    Same tone as the LLM output (direct, no fluff) but purely factual.

    2026-07-22 contract change: ALWAYS return a non-empty string. When
    source data is missing, return a stub that says so ("Данных нет" or
    similar) instead of None. Reason: /pult LLM-button must end with
    7/7 brief fields populated, not 2/7 — partial briefs are unhelpful
    to the user even when the underlying data is just unavailable.
    """
    facts = facts or {}
    if block_name == "weather":
        day = (facts or {}).get("day") or {}
        temp = day.get("temp") or day.get("temp_day")
        cond = day.get("condition") or day.get("condition_day")
        evening = (facts or {}).get("evening") or {}
        ev_temp = evening.get("temp") or evening.get("temp_evening")
        if not temp or not cond:
            return "Данных о погоде пока нет — посмотри на небо."
        parts = [f"Днём {temp}°, {cond.lower()}."]
        if ev_temp:
            try:
                d = round(float(ev_temp) - float(temp))
                if d <= -2:
                    parts.append(f"К вечеру на {abs(d)}° прохладнее.")
                elif d >= 2:
                    parts.append(f"К вечеру на {d}° теплее.")
                else:
                    parts.append("К вечеру без перепадов.")
            except (ValueError, TypeError):
                pass
        parts.append("Окно для дел на улице — пока светло.")
        return " ".join(parts)[:MAX_BLOCK_CHARS]

    if block_name == "tasks":
        facts = facts or {}
        n = facts.get("count", 0)
        p3 = facts.get("p3_count", 0)
        top = facts.get("top_task")
        if not n:
            return "Задач на сегодня нет — день свободен для главного."
        parts = [f"На сегодня {n} задач."]
        if p3:
            parts.append(f"Из них {p3} на p3 — это приоритет.")
        if top:
            parts.append(f"Топ: «{top}». С него и начинай.")
        else:
            parts.append("Начни с самой приоритетной.")
        return " ".join(parts)[:MAX_BLOCK_CHARS]

    if block_name == "movement":
        steps = facts.get("steps_yesterday")
        eaten = facts.get("kcal_eaten_yesterday")
        burned = facts.get("kcal_burned_yesterday")
        bal = facts.get("balance_yesterday")
        if steps is None:
            return "Данных о шагах за вчера нет — надень часы или запиши вручную."
        parts = [f"Вчера {steps} шагов."]
        if eaten is not None and burned is not None:
            parts.append(f"Съел {eaten}, потратил {burned} ккал.")
        if bal is not None:
            sign = "+" if bal >= 0 else ""
            parts.append(f"Баланс {sign}{bal} ккал.")
        if isinstance(steps, int) and steps < 7000:
            parts.append("Сегодня добери до 7к — две обычные прогулки.")
        elif isinstance(steps, int):
            parts.append("Темп держится, не сбавляй.")
        return " ".join(parts)[:MAX_BLOCK_CHARS]

    if block_name == "calendar":
        meetings = facts.get("meetings_count", 0)
        deepwork = facts.get("deepwork_minutes", 0)
        free = facts.get("free_day", False)
        if meetings == 0 and deepwork == 0:
            # Either explicit free day or no data — both read the same from
            # the user's POV: nothing in the calendar block.
            return "Встреч на сегодня нет — день твой, потрать его на главное."
        parts = [f"Встреч: {meetings}."]
        if deepwork:
            parts.append(f"Deep Work {deepwork} мин в утреннем окне.")
        parts.append("Между встречами — фокус на главном.")
        return " ".join(parts)[:MAX_BLOCK_CHARS]

    if block_name == "battery":
        bb = facts.get("body_battery")
        sleep_label = facts.get("sleep_label")
        sleep_score = facts.get("sleep_score")
        hrv = facts.get("hrv")
        if bb is None and sleep_label is None and hrv is None:
            return "Нет данных о батарейке и сне — заряди Garmin, обновим."
        parts = []
        if bb is not None:
            parts.append(f"Body Battery {bb}/100.")
        if sleep_label:
            s = f"Сон {sleep_label}"
            if sleep_score is not None:
                s += f" (score {sleep_score})"
            parts.append(s + ".")
        if hrv is not None:
            parts.append(f"HRV {hrv}.")
        parts.append("Ресурс ограничен — бери главное до обеда.")
        return " ".join(parts)[:MAX_BLOCK_CHARS]

    return f"Данных по блоку «{block_name}» пока нет."


# ── Async core ────────────────────────────────────────────────────────────
async def _compose_block_narrative_async(
    block_name: str,
    facts: dict[str, Any],
    *,
    timeout: int = 90,
) -> tuple[str, str | None, str]:
    """Fire one hermes -z call for block_name, return (text, source, error).

    Returns:
        (text_or_empty, source, error_msg)
        source ∈ {"llm", "fallback", "empty"}
    """
    hermes_bin = _resolve_hermes()
    if not hermes_bin:
        logger.warning("block-narrative[%s]: hermes missing (PATH=%s), using fallback",
                       block_name, os.environ.get("PATH", ""))
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", "hermes-missing")

    user_prompt = _format_user_prompt(block_name, facts)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{FEWSHOT}\n\n{user_prompt}"

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
        logger.warning("block-narrative[%s]: hermes timeout %ds, using fallback", block_name, timeout)
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", f"timeout-{timeout}s")

    if proc.returncode != 0:
        stderr = stderr_b.decode(errors="replace")[:200]
        logger.warning("block-narrative[%s]: exit %d, stderr=%s, using fallback",
                       block_name, proc.returncode, stderr)
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", f"rc={proc.returncode}")

    raw = stdout_b.decode("utf-8", errors="replace").strip()
    if not raw:
        logger.warning("block-narrative[%s]: empty stdout, using fallback", block_name)
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", "empty-stdout")

    # Defensive: strip markdown fences if LLM still wrapped output (we asked
    # for plain text, but be robust to legacy behaviour).
    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    # Plain-text mode (2026-07-18): LLM was asked to return a single paragraph
    # of Russian text directly, no JSON envelope. The previous JSON parse was
    # the dominant failure mode (M3 occasionally emits invalid JSON across
    # all 5 sequential calls — see job 197 journal on 2026-07-18 09:54).
    text = raw.strip()
    if len(text) > MAX_BLOCK_CHARS:
        text = text[: MAX_BLOCK_CHARS - 1].rstrip() + "…"
    if not text:
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", "empty-text")
    if _looks_like_error_text(text):
        # LLM returned an error/upstream message in plain-text mode (e.g.
        # "API call failed after 3 retries: HTTP 404"). Don't write garbage
        # to DB — fall back to deterministic template and mark it.
        logger.warning("block-narrative[%s]: LLM stdout looks like error: %r",
                       block_name, text[:200])
        fb = _fallback_block_text(block_name, facts)
        return (fb or "", "fallback" if fb else "empty", "llm-garbage-stdout")
    return (text, "llm", "")


def _looks_like_error_text(text: str) -> bool:
    """Heuristic: does the LLM stdout look like an upstream error or refusal
    rather than a real narrative?

    We asked for a Russian paragraph, 2-4 sentences, up to 500 chars. Common
    non-narrative patterns observed:
      - "API call failed after 3 retries: HTTP 404: 404 page not found"
      - "Error: ..." / "Exception: ..."
      - "I cannot help with that" / "I'm sorry, but ..."
      - Pure-English response (our tone is Russian)
      - Pure-punctuation / JSON-looking residue after fence-strip
    """
    if not text:
        return False
    t = text.strip()
    t_low = t.lower()
    # Explicit upstream / error patterns
    error_prefixes = (
        "api call failed", "api error", "http ",
        "error:", "exception:", "traceback", "failed to",
        "i cannot", "i can't", "i'm sorry", "i am sorry",
        "as an ai", "as a language model",
        "sorry,", "unfortunately,",
    )
    if any(t_low.startswith(p) for p in error_prefixes):
        return True
    # Pure-English response: no Cyrillic at all
    has_cyrillic = any("\u0400" <= ch <= "\u04FF" for ch in t)
    if not has_cyrillic and len(t) > 20:
        return True
    # JSON-looking residue (we stripped fences, but a stray "{...}" is suspicious)
    if t.startswith("{") and t.endswith("}"):
        return True
    return False


async def compose_block(block_name: str, facts: dict[str, Any], *, timeout: int = 90) -> str | None:
    """Single-block narrative — convenience wrapper. Returns text or None."""
    text, _source, _err = await _compose_block_narrative_async(block_name, facts, timeout=timeout)
    return text or None


async def compose_all_blocks(
    facts_by_block: dict[str, dict[str, Any]],
    *,
    timeout: int = 90,
) -> tuple[dict[str, str | None], dict[str, dict[str, Any]]]:
    """Sequentially compose 5 block narratives.

    Returns:
        ({block: text_or_None}, {block: meta}) where meta = {"source": ..., "error": ...}
    """
    out: dict[str, str | None] = {}
    meta: dict[str, dict[str, Any]] = {}
    for name in NARRATIVE_BLOCKS:
        facts = facts_by_block.get(name, {})
        text, source, err = await _compose_block_narrative_async(name, facts, timeout=timeout)
        out[name] = text or None
        meta[name] = {
            "source": source,
            "error": err or None,
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": "hermes-gateway",  # resolved by gateway; we don't pin
            "chars": len(text) if text else 0,
        }
    return out, meta