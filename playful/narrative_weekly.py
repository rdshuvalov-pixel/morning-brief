"""LLM-narrative for the WEEKLY recap (used by `weekly-recap` pult button).

Mirrors the architecture and conventions of `narrative.py`:
  facts dict -> system+user prompt -> hermes -z -> parse JSON -> dict

Differences from `narrative.py`:
  - Output schema is 5 fields: {headline, sleep, work, nutrition, next_week}
    instead of {headline, lead, footer_title, footer_text}.
  - Output target is TELEGRAM message (≤ 3500 chars) — much longer than the
    morning brief. Sections are 2-4 sentences each.
  - Tone: same (дерзкий, прямой, без армейской лексики).
  - "next_week" is the key section — LLM must produce concrete, checkable
    recommendations, not generic advice.

Failure modes (any of these returns None):
  - hermes binary not in PATH
  - subprocess timeout (default 120s)
  - non-zero exit
  - output doesn't parse as JSON
  - missing required keys
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты пишешь недельный обзор для пользователя, который отслеживает сон, продуктивность и питание.
Тон: дерзкий, прямой, короткие рубленые фразы. Никакой армейской лексики —
никаких «рядовой», «солдат», «сержант», «казарма», «в строй», «подъём».
Мотивация без пафоса. Конкретика из цифр, не абстракции.

Формат ответа СТРОГО — JSON с 5 полями:
{
  "headline":   "3-6 слов, провокация по итогам недели. Без точки.",
  "sleep":      "2-4 предложения. HRV/сон/recovery за неделю. Что тело накопило, чего недополучило.",
  "work":       "2-4 предложения. Задачи, встречи, динамика закрытия, движение (Σ шаги/км за неделю из Garmin). Что получилось, что просрал.",
  "nutrition":  "2-4 предложения. Калории, белок, режим питания. Где переедал/недоедал.",
  "next_week":  "3-5 предложений. КОНКРЕТНЫЕ рекомендации на следующую неделю. Не «больше спать», а «спать до 7:15 по будням, protein 140g/день». Каждая рекомендация — actionable, проверяемая, с числом."
}

ПРАВИЛА РАБОТЫ С ЧИСЛАМИ (КРИТИЧНО):
1. Ниже в user_prompt будет блок «ЗАФИКСИРОВАННЫЕ ЧИСЛА» — используй ТОЛЬКО эти числа.
2. Если число отсутствует (НЕТ ДАННЫХ) — НЕ выдумывай. Либо не упоминай поле, либо честно скажи «без данных по X».
3. НИКОГДА не округляй, не меняй и не придумывай альтернативные числа.
4. Все остальные данные (задачи, календарь, еда) бери строго из разделов user_prompt, не из общих знаний.
5. Если данных по разделу меньше, чем за 4 из 7 дней, отметь это явно: «по сну есть данные только за N из 7 дней».

ВАЖНО:
- «next_week» — самая важная секция. Избегай общих слов вроде «больше отдыхай», «питайся лучше». Только конкретные, числовые, проверяемые рекомендации.
- Суммарный объём текста должен быть ≤ 3500 символов (влезет в одно TG-сообщение).

Никаких префиксов вроде «Вот JSON:» — только валидный JSON.
"""


FEW_SHOT = """Примеры эталона тона (НЕ копируй дословно):

headline: "Неделя в режиме марафонца"
sleep: "HRV держался 65-72, сон в среднем 7ч 12м — тело держалось. В среду просел до 58, восстановился к пятнице. Body Battery в среднем 71 — норма для твоей нагрузки."
work: "12 задач, 5 закрыто. Две p1 так и висят — «Календарь миграция» и «CRM план-Б». Встреч 18 часов за неделю, среда — самый загруженный день (6ч)."
nutrition: "Калории держал 2100-2400, белок проседал: три дня по 90-110g при норме 140. Воскресный читмил ушёл в 3100 — зачёт."
next_week: "1. Белок — минимум 130g/день, поставь напоминалку на 13:00. 2. Закрой «CRM план-Б» до среды, иначе неделя повторится. 3. Среда — самая загруженная, планируй вторник под её подготовку."
"""


def _line(facts: dict[str, Any], key: str, label: str, fmt: str = "{}") -> str:
    v = facts.get(key)
    if v is None or v == "":
        return f"  - {label}: НЕТ ДАННЫХ"
    try:
        return f"  - {label}: {fmt.format(v)}"
    except Exception:
        return f"  - {label}: {v}"


def _format_user_prompt(facts: dict[str, Any]) -> str:
    """Build a compact user prompt with weekly aggregates as facts."""
    lines = [f"Неделя: {facts.get('week_range', 'unknown')}."]
    lines.append(f"Сегодня (запрос от): {facts.get('request_date', 'unknown')}.")
    lines.append(f"Дней с данными: {facts.get('days_with_data', '?')}/7.")
    lines.append("")

    # Garmin (weekly)
    g = facts.get("garmin_weekly") or {}
    lines.append("Garmin (средние за неделю, дней с данными скобкой):")
    if not g:
        lines.append("  - НЕТ ДАННЫХ по всей неделе")
    else:
        lines.append(f"  - Дней с данными (сон/HRV): {g.get('days', 'НЕТ ДАННЫХ')}/7")
        lines.append(_line(g, "mean_sleep_min", "Сон — средний (минут)"))
        lines.append(_line(g, "mean_sleep_score", "Sleep Score — средний"))
        lines.append(_line(g, "mean_hrv", "HRV — средний (мс)"))
        lines.append(_line(g, "mean_rhr", "Пульс покоя — средний"))
        lines.append(_line(g, "mean_body_battery", "Body Battery — среднее"))
        lines.append(_line(g, "mean_stress", "Стресс — средний"))
        lines.append(_line(g, "min_deep_pct", "Deep sleep — минимум за неделю (%)"))
        # Movement
        lines.append(_line(g, "sum_steps",
                           "Шаги за неделю (Σ). Дней с данными: " +
                           str(g.get("steps_days_with_data", "?")) + "/7"))
        lines.append(_line(g, "mean_steps", "Шаги — среднее в день"))
        lines.append(_line(g, "sum_distance_km",
                           "Километры за неделю (Σ). Дней: " +
                           str(g.get("distance_days_with_data", "?")) + "/7"))
        lines.append(_line(g, "mean_distance_km", "Километры — среднее в день"))
    lines.append("")

    # Питание
    f = facts.get("food_weekly") or {}
    lines.append("Питание (агрегаты за неделю):")
    if not f:
        lines.append("  - НЕТ ДАННЫХ (food_log пуст за неделю)")
    else:
        lines.append(f"  - Дней с записями: {f.get('log_days', 'НЕТ ДАННЫХ')}/7")
        lines.append(_line(f, "sum_kcal", "Калории — всего за неделю"))
        lines.append(_line(f, "mean_kcal_per_day", "Калории — среднее в день"))
        lines.append(_line(f, "mean_protein_g", "Белок — среднее в день (g)"))
        lines.append(_line(f, "top_meal_by_kcal", "Самая калорийная еда за неделю"))
        lines.append(_line(f, "cheat_day_kcal", "Самый «тяжёлый» день — ккал"))
    lines.append("")

    # Календарь (встречи)
    c = facts.get("calendar_weekly") or {}
    lines.append("Календарь (встречи за неделю):")
    if not c:
        lines.append("  - НЕТ ДАННЫХ (calendar_events пуст)")
    else:
        lines.append(_line(c, "total_meetings", "Всего встреч"))
        lines.append(_line(c, "total_minutes", "Суммарно минут"))
        lines.append(_line(c, "busiest_day", "Самый загруженный день"))
        lines.append(_line(c, "longest_meeting", "Самая длинная встреча (title, мин)"))
    lines.append("")

    # Задачи
    t = facts.get("tasks_weekly") or {}
    lines.append("Задачи (динамика закрытия):")
    if not t:
        lines.append("  - НЕТ ДАННЫХ (tasks пуст)")
    else:
        lines.append(_line(t, "total_unique", "Всего уникальных задач в логах"))
        lines.append(_line(t, "p1_total", "P1 — всего"))
        lines.append(_line(t, "p2_total", "P2 — всего"))
        lines.append(_line(t, "p3_total", "P3 — всего"))
    lines.append("")

    # Trends (если есть прошлая неделя в БД)
    tr = facts.get("trends") or {}
    if tr:
        lines.append("Динамика vs предыдущая неделя:")
        for k, v in tr.items():
            lines.append(f"  - {k}: {v}")
        lines.append("")

    return "\n".join(lines)


def compose(facts: dict[str, Any], *, timeout: int = 120) -> dict[str, str] | None:
    """Generate weekly recap narrative via Hermes LLM.

    Args:
        facts: dict from generate_weekly_recap.py. Must contain 'week_range'
            and at least one of garmin_weekly/food_weekly/calendar_weekly/tasks_weekly.
        timeout: subprocess timeout (default 120s).

    Returns:
        dict with {headline, sleep, work, nutrition, next_week} or None on failure.
    """
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        logger.warning("narrative_weekly: 'hermes' binary not in PATH, skipping LLM call")
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
        logger.warning("narrative_weekly: hermes -z timed out after %ds", timeout)
        return None
    except Exception as e:
        logger.warning("narrative_weekly: subprocess failed: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning("narrative_weekly: hermes exit %d, stderr=%s", proc.returncode, proc.stderr[:200])
        return None

    raw = proc.stdout.strip()
    if not raw:
        logger.warning("narrative_weekly: hermes returned empty stdout")
        return None

    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("narrative_weekly: JSON parse failed: %s; raw=%r", e, raw[:300])
        return None

    required = {"headline", "sleep", "work", "nutrition", "next_week"}
    if not isinstance(data, dict) or not required.issubset(data):
        logger.warning("narrative_weekly: missing keys %s in %s", required - set(data or {}), list(data or {}))
        return None

    out = {k: str(data[k]).strip() for k in required}

    # Send any empty/None fields back as "—" so renderer handles it gracefully
    for k in required:
        if not out[k]:
            out[k] = "—"

    return out


def render_for_telegram(out: dict[str, str], *, week_range: str) -> str:
    """Format the 5-field dict as a single Telegram message (markdown).

    Splits at 3500 chars if needed (TG limit 4096 with margin).
    """
    parts = []
    parts.append(f"📊 *Weekly Recap — {week_range}*")
    parts.append("")
    parts.append(f"*{out.get('headline', '—').strip()}*")
    parts.append("")
    parts.append(f"*🛌 Сон и recovery*\n{out.get('sleep', '—').strip()}")
    parts.append("")
    parts.append(f"*💼 Работа и задачи*\n{out.get('work', '—').strip()}")
    parts.append("")
    parts.append(f"*🍽 Питание*\n{out.get('nutrition', '—').strip()}")
    parts.append("")
    parts.append(f"*🎯 На следующую неделю*\n{out.get('next_week', '—').strip()}")
    parts.append("")
    parts.append("— morning_brief_v2 · weekly-recap")

    text = "\n".join(parts)

    # If somehow over budget, hard-truncate last section
    if len(text) > 3800:
        # Find last double-newline before 3800
        cut = text.rfind("\n\n", 0, 3800)
        if cut < 100:
            cut = 3800
        text = text[:cut].rstrip() + "\n\n[… обрезано, полная версия в /pult → weekly-recap]"

    return text


def render_for_telegram_pages(out: dict[str, str], *, week_range: str) -> list[str]:
    """Pack the 5 sections into ≤3500-char pages, splitting at section boundaries.

    Always uses `_split_into_pages` (NOT `render_for_telegram`'s truncated form),
    so output is deterministic regardless of whether total fits one message.
    """
    return _split_into_pages(out, week_range)


def _split_into_pages(out: dict[str, str], week_range: str) -> list[str]:
    """Pack the 5 sections into ≤3500-char pages, splitting at section boundaries.

    Always works from the structured sections (NOT from `render_for_telegram`'s
    pre-truncated string), so it correctly handles cases where total exceeds
    3500 chars after assembling all sections.
    """
    sections = [
        f"📊 *Weekly Recap — {week_range}*",
        f"*{out.get('headline', '—').strip()}*",
        f"*🛌 Сон и recovery*\n{out.get('sleep', '—').strip()}",
        f"*💼 Работа и задачи*\n{out.get('work', '—').strip()}",
        f"*🍽 Питание*\n{out.get('nutrition', '—').strip()}",
        f"*🎯 На следующую неделю*\n{out.get('next_week', '—').strip()}",
        "— morning_brief_v2 · weekly-recap",
    ]
    pages: list[str] = []
    cur = ""
    for s in sections:
        # If a single section > 3500, it has to be hard-cut
        if len(s) > 3500:
            if cur:
                pages.append(cur.rstrip())
                cur = ""
            for i in range(0, len(s), 3500):
                pages.append(s[i:i + 3500])
            continue
        candidate = (cur + "\n\n" + s).strip() if cur else s
        if len(candidate) > 3500:
            pages.append(cur.rstrip())
            cur = s
        else:
            cur = candidate
    if cur:
        pages.append(cur.rstrip())
    return pages
