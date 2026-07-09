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
# Make sure this module's logs reach the root logger (so basicConfig from
# generate_weekly_recap.py captures them in systemd journal / stderr).
logger.propagate = True


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


REQUIRED_FIELDS = ("headline", "sleep", "work", "nutrition", "next_week")
# Each field's minimum length to count as "real content". Below this we treat
# the field as empty and reject the whole payload (forces retry rather than
# shipping a half-blank recap).
_MIN_FIELD_LEN = {
    "headline": 8,   # at least "что-то тут" or 3-6 word headline
    "sleep": 40,
    "work": 40,
    "nutrition": 40,
    "next_week": 60,  # must be a real recommendation block, not a one-liner
}


def _call_hermes(hermes_bin: str, prompt: str, *, timeout: int, attempt: int) -> tuple[str | None, str]:
    """Call `hermes -z <prompt>` once. Returns (stdout_or_None, reason).

    `reason` is one of: "ok" | "timeout" | "non-zero" | "empty" | "exception".
    """
    try:
        proc = subprocess.run(
            [hermes_bin, "-z", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "narrative_weekly: attempt=%d TIMEOUT after %ds (prompt %d chars)",
            attempt, timeout, len(prompt))
        return None, "timeout"
    except Exception as e:
        logger.warning(
            "narrative_weekly: attempt=%d exception: %s: %s",
            attempt, type(e).__name__, e)
        return None, "exception"

    if proc.returncode != 0:
        logger.warning(
            "narrative_weekly: attempt=%d rc=%d stderr=%r stdout_head=%r",
            attempt, proc.returncode,
            proc.stderr[:200], proc.stdout[:200])
        return None, "non-zero"

    raw = proc.stdout.strip()
    if not raw:
        logger.warning(
            "narrative_weekly: attempt=%d empty stdout", attempt)
        return None, "empty"

    return raw, "ok"


def _parse_response(raw: str, *, attempt: int) -> dict[str, str] | None:
    """Parse LLM response into the 5-field dict. Strict: rejects half-filled or empty fields.

    Returns None on any failure (caller will retry).
    """
    # Strip markdown fence wrapper if present.
    cleaned = raw
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(
            "narrative_weekly: attempt=%d JSON parse failed: %s; raw=%r",
            attempt, e, raw[:300])
        return None

    if not isinstance(data, dict) or not set(REQUIRED_FIELDS).issubset(data):
        missing = set(REQUIRED_FIELDS) - set(data or {})
        logger.warning(
            "narrative_weekly: attempt=%d missing keys=%s in keys=%s",
            attempt, missing, list(data or {}))
        return None

    # Coerce to stripped strings and check minimal content length.
    out: dict[str, str] = {}
    short: list[tuple[str, int]] = []
    for k in REQUIRED_FIELDS:
        v = str(data.get(k) or "").strip()
        if len(v) < _MIN_FIELD_LEN[k]:
            short.append((k, len(v)))
        out[k] = v

    if short:
        logger.warning(
            "narrative_weekly: attempt=%d too-short fields=%s (min_len=%s)",
            attempt, short, _MIN_FIELD_LEN)
        return None

    return out


def _short_user_prompt(facts: dict[str, Any]) -> str:
    """Compact user prompt for retry — only ЗАФИКСИРОВАННЫЕ ЧИСЛА, no FEW_SHOT.

    Used when the first attempt fails (timeout / parse error / half-filled).
    Shorter prompts respond faster and parse more reliably.
    """
    g = facts.get("garmin_weekly") or {}
    f = facts.get("food_weekly") or {}
    c = facts.get("calendar_weekly") or {}
    t = facts.get("tasks_weekly") or {}
    tr = facts.get("trends") or {}
    lines = [
        "Недельный обзор. JSON с 5 полями:",
        '  "headline": "3-6 слов, провокация. Без точки.",',
        '  "sleep": "2-4 предложения по сну/HRV/BB.",',
        '  "work": "2-4 предложения по задачам/встречам/шагам.",',
        '  "nutrition": "2-4 предложения по калориям/белку.",',
        '  "next_week": "3-5 конкретных рекомендаций с числами.",',
        "Только числа ниже. Никаких префиксов. Только валидный JSON.",
        f"Неделя: {facts.get('week_range', '?')}",
        f"HRV mean={g.get('mean_hrv')} SleepScore mean={g.get('mean_sleep_score')} "
        f"BB mean={g.get('mean_body_battery')} Sleep mean_min={g.get('mean_sleep_min')} "
        f"RHR mean={g.get('mean_rhr')} DeepMin={g.get('min_deep_pct')}",
        f"Steps sum={g.get('sum_steps')} mean/day={g.get('mean_steps')} "
        f"Distance sum_km={g.get('sum_distance_km')}",
        f"Kcal mean/day={f.get('mean_kcal_per_day')} Protein g/day={f.get('mean_protein_g')} "
        f"CheatDay={f.get('cheat_day_kcal')} TopMeal={f.get('top_meal_by_kcal')}",
        f"Meetings={c.get('total_meetings')} min={c.get('total_minutes')} "
        f"busy={c.get('busiest_day')}",
        f"Tasks unique={t.get('total_unique')} p1={t.get('p1_total')} "
        f"p2={t.get('p2_total')} p3={t.get('p3_total')}",
    ]
    if tr:
        lines.append("Trends: " + " | ".join(f"{k}={v}" for k, v in tr.items()))
    return "\n".join(lines)


def compose(facts: dict[str, Any], *, timeout: int = 180) -> dict[str, str] | None:
    """Generate weekly recap narrative via Hermes LLM.

    Two-attempt strategy:
      1. Full prompt (system + few-shot + user prompt) at `timeout` seconds.
      2. If attempt 1 returns None, retry with a compact prompt (numbers only,
         no FEW_SHOT) at the same timeout. Both attempts log their reason.

    Args:
        facts: dict from generate_weekly_recap.py. Must contain 'week_range'
            and at least one of garmin_weekly/food_weekly/calendar_weekly/tasks_weekly.
        timeout: subprocess timeout per attempt (default 180s).

    Returns:
        dict with {headline, sleep, work, nutrition, next_week} or None on failure.
    """
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        logger.warning("narrative_weekly: 'hermes' binary not in PATH, skipping LLM call")
        return None

    # Attempt 1: full prompt.
    full_prompt = f"{SYSTEM_PROMPT}\n\n{FEW_SHOT}\n\n{_format_user_prompt(facts)}"
    raw, why = _call_hermes(hermes_bin, full_prompt, timeout=timeout, attempt=1)
    if raw is not None:
        out = _parse_response(raw, attempt=1)
        if out is not None:
            logger.info("narrative_weekly: attempt=1 ok (%d chars headline)", len(out["headline"]))
            return out

    # Attempt 2: compact retry. Skip if first failure was non-recoverable
    # (non-zero exit, hermes missing) — but we already caught that above, so
    # retry covers timeout/empty/JSON-parse/half-filled.
    logger.info("narrative_weekly: attempt=1 failed (%s), retrying with compact prompt", why)
    short_prompt = _short_user_prompt(facts)
    raw, why2 = _call_hermes(hermes_bin, short_prompt, timeout=timeout, attempt=2)
    if raw is not None:
        out = _parse_response(raw, attempt=2)
        if out is not None:
            logger.info("narrative_weekly: attempt=2 ok (%d chars headline)", len(out["headline"]))
            return out

    logger.warning("narrative_weekly: both attempts failed (last reason: %s)", why2)
    return None


def _format_summary_block(facts: dict[str, Any] | None) -> str:
    """Compact numerical summary from the weekly facts dict.

    Rendered AFTER the LLM narrative (always in LLM mode, never in fallback).
    ~8 lines, intended for at-a-glance comparison between weeks. Built from
    the SAME facts dict that goes into compose(), so numbers cannot drift.
    """
    if not facts:
        return ""

    g = facts.get("garmin_weekly") or {}
    f = facts.get("food_weekly") or {}
    c = facts.get("calendar_weekly") or {}
    t = facts.get("tasks_weekly") or {}
    tr = facts.get("trends") or {}

    def _g(key, default="—"):
        v = g.get(key)
        return default if v is None else str(v)

    def _f(key, default="—"):
        v = f.get(key)
        return default if v is None else str(v)

    def _c(key, default="—"):
        v = c.get(key)
        return default if v is None else str(v)

    def _t(key, default="—"):
        v = t.get(key)
        return default if v is None else str(v)

    lines = [
        "📈 *Сводка*",
        f"🛌 {_g('mean_sleep_min')} мин сна · HRV {_g('mean_hrv')} "
        f"· BB {_g('mean_body_battery')} · SS {_g('mean_sleep_score')}",
        f"🍽 {_f('mean_kcal_per_day')} ккал · {_f('mean_protein_g')}g белка "
        f"({_f('log_days')}/7 дней)",
        f"📅 {_c('total_meetings')} встреч {_c('total_minutes')} мин "
        f"· busy {_c('busiest_day')}",
        f"✅ {_t('total_unique')} задач (P1 {_t('p1_total')}, "
        f"P2 {_t('p2_total')}, P3 {_t('p3_total')})",
    ]
    if tr:
        lines.append("")
        lines.append("*vs прошлая:*")
        for k, v in tr.items():
            lines.append(f"  {v}")
    return "\n".join(lines)


def render_for_telegram(out: dict[str, str], *, week_range: str,
                        facts: dict[str, Any] | None = None) -> str:
    """Format the 5-field dict as a single Telegram message (markdown).

    Splits at 3500 chars if needed (TG limit 4096 with margin).
    If `facts` is provided, appends a compact numerical summary block AFTER
    the LLM narrative so the user gets both prose and at-a-glance numbers.
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
    summary = _format_summary_block(facts)
    if summary:
        parts.append("")
        parts.append(summary)
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


def render_for_telegram_pages(out: dict[str, str], *, week_range: str,
                              facts: dict[str, Any] | None = None) -> list[str]:
    """Pack the 5 sections into ≤3500-char pages, splitting at section boundaries.

    Always uses `_split_into_pages` (NOT `render_for_telegram`'s truncated form),
    so output is deterministic regardless of whether total fits one message.
    If `facts` is provided, the summary block is appended to the FINAL page so
    it lands next to the narrative footer rather than splitting across pages.
    """
    return _split_into_pages(out, week_range, facts=facts)


def _split_into_pages(out: dict[str, str], week_range: str,
                     facts: dict[str, Any] | None = None) -> list[str]:
    """Pack the 5 sections into ≤3500-char pages, splitting at section boundaries.

    Always works from the structured sections (NOT from `render_for_telegram`'s
    pre-truncated string), so it correctly handles cases where total exceeds
    3500 chars after assembling all sections. Appends summary block (if `facts`
    given) to the final page alongside the footer.
    """
    sections = [
        f"📊 *Weekly Recap — {week_range}*",
        f"*{out.get('headline', '—').strip()}*",
        f"*🛌 Сон и recovery*\n{out.get('sleep', '—').strip()}",
        f"*💼 Работа и задачи*\n{out.get('work', '—').strip()}",
        f"*🍽 Питание*\n{out.get('nutrition', '—').strip()}",
        f"*🎯 На следующую неделю*\n{out.get('next_week', '—').strip()}",
    ]
    summary = _format_summary_block(facts)
    if summary:
        # Summary goes between narrative and footer so the footer is the LAST line.
        sections.append(summary)
    sections.append("— morning_brief_v2 · weekly-recap")

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
