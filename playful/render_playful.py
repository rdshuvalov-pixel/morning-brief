"""Production Playful — renderer для утреннего брифа по спеке v2.

Не лезет в существующий pipeline (renderer.py / narrator.py / brief_builder.py).
Самостоятельный модуль, который:
- собирает свой context из BriefContext (если есть) или из demo-фикстур,
- гоняет через Jinja-шаблон playful/brief_playful.html.j2,
- пишет готовый HTML на диск.

Использование:
    python -m playful.render_playful --demo            # demo-фикстуры → /tmp/brief_demo.html
    python -m playful.render_playful --from-json X.json # из готового JSON
    python -m playful.render_playful --live --date YYYY-MM-DD   # из БД morning_brief_v2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

# Путь к шаблону
_HERE = Path(__file__).parent
_TEMPLATES = _HERE  # brief_playful.html.j2 лежит рядом
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
)


# ─────────────────────────────────────────────────────────────────────
# Утилиты форматирования
# ─────────────────────────────────────────────────────────────────────

# Маппинг weather condition → emoji (концепт)
_WEATHER_EMOJI = {
    "clear": "☀️",
    "clouds": "🌤️",
    "rain": "🌧️",
    "drizzle": "🌦️",
    "thunderstorm": "⛈️",
    "snow": "❄️",
    "wind": "🌬️",
    "mist": "🌫️",
    "fog": "🌫️",
}


def _minutes_to_label(minutes: int | None) -> str:
    """462 → '7ч 42м', 90 → '1ч 30м', None → '—'."""
    if minutes is None:
        return "—"
    h = minutes // 60
    m = minutes % 60
    return f"{h}ч {m:02d}м"


def _pct(part: int | None, total: int | None) -> int:
    if not part or not total:
        return 0
    return round(part / total * 100)


def _format_num(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")


def _battery_color(bb: int | float | None) -> str:
    """Цвет кольца и pill по body_battery: ≥90 good, 75-89 amber, <75 rose."""
    if bb is None:
        return "rose"
    if bb >= 90:
        return "good"
    if bb >= 75:
        return "amber"
    return "rose"


def _battery_color_track(color: str) -> str:
    """Цвет незаполненной части кольца (низкая прозрачность того же цвета)."""
    return {
        "good": "rgba(45,155,125,0.14)",
        "amber": "rgba(211,138,44,0.14)",
        "rose": "rgba(240,103,87,0.14)",
    }[color]


def _stress_label_and_color(stress: int | None) -> tuple[str, str, str]:
    """(value_label, pill_class, meta). stress ≤25 Low, 26-50 Med, >50 High."""
    if stress is None:
        return ("—", "pill-muted", "нет данных")
    if stress <= 25:
        return ("Low", "pill-good", "Плавный старт без шума")
    if stress <= 50:
        return ("Med", "pill-amber", "Средний фон")
    return ("High", "pill-rose", "Шумный день был")


def _readiness_label_and_color(tr: int | None) -> tuple[str, str]:
    """(value_label, pill_class). ≥75 High, 50-74 Medium, <50 Low."""
    if tr is None:
        return ("—", "pill-muted")
    if tr >= 75:
        return ("High", "pill-blue")
    if tr >= 50:
        return ("Medium", "pill-amber")
    return ("Low", "pill-rose")


def _sleep_pill(score: int | None) -> tuple[str, str]:
    """≥90 good / 75-89 amber / <75 rose. ('Night Stable', 'pill-good')."""
    if score is None:
        return ("Night —", "pill-muted")
    if score >= 90:
        return ("Night Stable", "pill-good")
    if score >= 75:
        return ("Night Uneven", "pill-amber")
    return ("Rough Night", "pill-rose")


def _hrv_status(hrv: int | None, baseline: int | None) -> str:
    """Сравнение с базой: 'Выше базы' / 'В пределах' / 'Ниже базы'."""
    if hrv is None or baseline is None or baseline == 0:
        return "—"
    diff_pct = (hrv - baseline) / baseline * 100
    if diff_pct >= 5:
        return "Выше базы"
    if diff_pct >= -5:
        return "В пределах"
    return "Ниже базы"


def _hrv_baseline_from_7d(values: list[int]) -> int | None:
    if not values:
        return None
    return round(sum(values) / len(values))


def _activity_pill(steps: int | None, steps_goal: int, balance: int) -> str:
    """Keep It Light / On Track / Move More по твоей логике."""
    if steps is None:
        return "—"
    ratio = steps / steps_goal
    if ratio >= 1.0:
        return "Move More"
    if ratio >= 0.8 and abs(balance) <= 300:
        return "On Track"
    return "Keep It Light"


def _format_duration(minutes: int | None) -> str:
    """Минуты → '1ч 30м' / '45 мин' / '1ч 00м'."""
    if minutes is None:
        return ""
    if minutes >= 60:
        h = minutes // 60
        m = minutes % 60
        if m == 0:
            return f"{h}ч"
        return f"{h}ч {m:02d}м"
    return f"{minutes} мин"


def _build_sleep_pcts(
    sleep_duration_min: int | None,
    deep_sleep_pct: float | None,
) -> tuple[int, int, int, int]:
    """Возвращает (deep_pct, rem_pct, light_pct, awake_pct) для бара.
    Если deep_sleep_pct задан, остальное распределяем пропорционально
    стандартным Garmin-долям (rem ~22%, awake ~5%). Иначе — fallback
    из concept-production-playful (35/23/42 + 0 awake).
    """
    if sleep_duration_min is None:
        return 35, 23, 42, 0

    if deep_sleep_pct is not None and deep_sleep_pct > 0:
        deep = round(deep_sleep_pct)
        # Если bar показывает только 3 сегмента, не делим на awake
        rem = 22
        light = max(0, 100 - deep - rem - 3)
        awake = 3
        return deep, rem, light, awake

    return 35, 23, 42, 0


def _hrv_trend_svg(values: list[int]) -> tuple[str, str]:
    """Строит path для спарклайна HRV за 7 дней.
    Возвращает (path_line, path_area). Если данных < 3 — placeholder."""
    if not values or len(values) < 3:
        # Placeholder из концепта
        return (
            "M4 38 C16 34, 22 28, 34 30 S56 20, 70 24 S92 34, 116 18",
            "M4 44 C16 40, 22 34, 34 36 S56 26, 70 30 S92 40, 116 24 L116 52 L4 52 Z",
        )

    # Нормализуем значения в диапазон 0..52 (высота svg)
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        vmax = vmin + 1  # избегаем деления на ноль
    width, height = 120, 52

    pts = []
    for i, v in enumerate(values):
        x = 4 + (width - 8) * (i / (len(values) - 1))
        y = height - 8 - (height - 16) * ((v - vmin) / (vmax - vmin))
        pts.append((x, y))

    # Строим сглаженную кривую (простая cubic spline через control points)
    line = f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"
    for i in range(1, len(pts)):
        x_prev, y_prev = pts[i - 1]
        x_cur, y_cur = pts[i]
        cx1 = x_prev + (x_cur - x_prev) * 0.4
        cy1 = y_prev
        cx2 = x_cur - (x_cur - x_prev) * 0.4
        cy2 = y_cur
        line += f" C{cx1:.1f} {cy1:.1f}, {cx2:.1f} {cy2:.1f}, {x_cur:.1f} {y_cur:.1f}"

    # Area — закрываем кривую к низу
    area = line + f" L{pts[-1][0]:.1f} {height} L{pts[0][0]:.1f} {height} Z"
    return line, area


def _build_deep_work_slots(
    events: list[dict],
    body_battery: int | float | None,
    sleep_score: int | None,
    work_day_start: str = "08:00",
) -> list[dict]:
    """Вставляет Deep Work слот:
    - в утреннее окно до ПЕРВОЙ встречи (если до неё ≥60 мин и ресурс высокий),
    - между встречами только если окно ≥120 мин и сейчас <12:00 (т.е. утреннее фокусное).

    Deep Work — ОДНО рекомендованное окно в день (обычно утром). Не плодим дубли.

    Возвращает расширенный список events с type='meeting' | 'deepwork'.
    """
    # Подготовим копию событий с парсингом start_time в минутах от полуночи
    parsed = []
    for e in sorted(events, key=lambda x: x.get("start_time") or "99:99"):
        st = e.get("start_time")
        if not st or ":" not in st:
            parsed.append({**e, "_start_min": None, "_type": "meeting"})
            continue
        try:
            hh, mm = st.split(":")[:2]
            parsed.append({
                **e,
                "_start_min": int(hh) * 60 + int(mm),
                "_type": "meeting",
            })
        except ValueError:
            parsed.append({**e, "_start_min": None, "_type": "meeting"})

    resource_high = (
        body_battery is not None and body_battery >= 70
        and sleep_score is not None and sleep_score >= 80
    )

    work_start_min = 8 * 60  # 08:00
    noon_min = 12 * 60       # 12:00 — после этого Deep Work не вставляем
    result: list[dict] = []
    prev_end_min = work_start_min
    deepwork_inserted = False  # гейт — только ОДИН Deep Work в день

    for e in parsed:
        cur_start = e.get("_start_min")
        if cur_start is None:
            result.append({**e, "css_class": ""})
            prev_end_min = None
            continue

        gap = cur_start - (prev_end_min or work_start_min)

        # Решаем, вставлять ли Deep Work
        if not deepwork_inserted and gap >= 60:
            # Утреннее окно: до первой встречи до 12:00 ИЛИ большой gap ≥120 мин
            is_morning = (prev_end_min or work_start_min) < noon_min
            big_enough = gap >= 120
            if (resource_high and is_morning) or big_enough:
                result.append({
                    "title": "Deep Work",
                    "description": "главная задача без переключений",
                    "time_primary": f"{prev_end_min // 60:02d}:{prev_end_min % 60:02d}" if prev_end_min else work_day_start,
                    "time_secondary": f"{gap} мин",
                    "css_class": "item-deepwork",
                    "_type": "deepwork",
                })
                deepwork_inserted = True

        # Сама встреча
        duration = e.get("duration_minutes")
        time_secondary = _format_duration(duration) if duration else ""
        result.append({
            **e,
            "time_secondary": time_secondary,
            "css_class": "",
        })

        if duration:
            prev_end_min = cur_start + duration
        else:
            prev_end_min = cur_start + 30  # дефолт если длительность неизвестна

    # Лимит: топ-5 строк суммарно (приоритет встречам)
    if len(result) > 5:
        meetings = [r for r in result if r.get("_type") == "meeting"]
        deeps = [r for r in result if r.get("_type") == "deepwork"]
        kept: list[dict] = []
        for r in result:
            if r.get("_type") == "deepwork":
                if r in deeps[:1]:  # максимум 1 deep work
                    kept.append(r)
            else:
                kept.append(r)
                if len([k for k in kept if k.get("_type") == "meeting"]) >= 4:
                    break
        result = kept[:5]

    return result


def _build_weather_vs_yesterday(today: list[dict], yesterday: list[dict] | None) -> str:
    """Генерит текст сравнения с предыдущим днём."""
    if not yesterday or len(yesterday) != len(today):
        return ""

    diffs = []
    for t, y in zip(today, yesterday):
        period = t.get("period", "")
        t_temp = t.get("temp")
        y_temp = y.get("temp")
        if t_temp is None or y_temp is None:
            continue
        d = round(t_temp - y_temp)
        period_ru = {"morning": "утром", "day": "днём", "evening": "вечером"}.get(period, period)
        if d >= 2:
            diffs.append(f"{period_ru} на {d}° теплее")
        elif d <= -2:
            diffs.append(f"{period_ru} на {abs(d)}° прохладнее")
        else:
            diffs.append(f"{period_ru} так же")

    # Ветер (по максимуму за день)
    y_wind = max((y.get("wind") or 0 for y in yesterday), default=0)
    t_wind = max((t.get("wind") or 0 for t in today), default=0)
    if y_wind and t_wind:
        wd = round((t_wind - y_wind) * 3.6)  # м/с → км/ч
        if abs(wd) >= 5:
            diffs.append(f"ветер {'сильнее' if wd > 0 else 'слабее'} на {abs(wd)} км/ч")

    if not diffs:
        return "Похоже на вчера."

    return "; ".join(diffs) + "."


# ─────────────────────────────────────────────────────────────────────
# Сборка контекста
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PlayfulContext:
    """Плоский контекст для Jinja-шаблона. Каждое поле уже отформатировано."""
    # Header
    date_label: str
    headline: str
    headline_summary: str

    # Battery ring
    battery_value: str
    battery_color: str          # 'good' | 'amber' | 'rose'
    battery_deg: int
    battery_track: str
    battery_source_label: str    # "" если данные за сегодня, иначе "по данным 26 June"

    # Hero-grid
    sleep_label: str
    sleep_score: str
    hrv_value: str
    hrv_status: str
    rhr_value: str

    # Sleep stages
    sleep_deep_pct: int
    sleep_rem_pct: int
    sleep_light_pct: int
    sleep_awake_pct: int
    sleep_deep_label: str
    sleep_rem_label: str
    sleep_light_label: str
    sleep_awake_label: str
    sleep_start_label: str
    sleep_mid_label: str
    sleep_end_label: str
    sleep_pill_class: str
    sleep_pill_text: str

    # 2c SpO2 + HRV-trend
    spo2_value: str
    spo2_meta: str
    spo2_source_label: str    # "" если данные за сегодня, иначе "по данным 26 June"
    spo2_value_square: str
    hrv_trend_path: str
    hrv_trend_area: str

    # 3b metrics
    body_battery_delta: int | None
    body_battery_delta_label: str
    body_battery_delta_class: str
    readiness_label: str
    readiness_meta: str
    stress_label: str
    stress_meta: str
    spo2_meta_short: str
    focus_window_label: str

    # 4/1 agenda
    meeting_count_label: str
    agenda_items: list[dict]

    # 4/2 tasks
    tasks: list[dict]
    tasks_count_label: str

    # 5 movement
    steps_value: str
    steps_goal: int
    steps_pct: int
    kcal_burned: str
    kcal_burned_pct: int
    kcal_eaten: str
    kcal_eaten_pct: int
    balance_meta: str
    activity_pill: str

    # 6 weather
    wind_pill_label: str
    weather_periods: list[dict]
    weather_vs_yesterday: str

    # 7 footer
    footer_title: str
    footer_text: str


def build_playful_context(
    *,
    brief_date: date,
    garmin: dict | None,
    helio: dict | None,
    food: list[dict],
    weather: list[dict],
    calendar: list[dict],
    tasks: list[dict],
    weather_yesterday: list[dict] | None = None,
    narrative_headline: str = "Доброе утро",
    narrative_summary: str = "Утренний бриф собирается...",
    narrative_footer_title: str = "Утренний бриф",
    narrative_footer_text: str = "Данные собираются...",
    focus_window: str = "Focus 08:30–11:30",
    hrv_baseline: int | None = None,
    hrv_7d: list[int] | None = None,
    body_battery_delta: int | None = None,
    body_battery_yesterday: int | float | None = None,
    stress_yesterday: int | None = None,
    spo2_yesterday: float | None = None,
) -> PlayfulContext:
    """Собрать PlayfulContext из сырых полей.

    Поля garmin/helio — словари (или None) с теми же именами, что в GarminData/HelioData.
    food — список словарей {meal_name, kcal, ...}.
    weather — список словарей {period, temp, condition, wind} (morning/day/evening).
    calendar — список словарей {title, start_time, duration_minutes, end_time}.
    tasks — список словарей {title, priority, due_time}.
    """

    # ── Date label "27 June" ──
    date_label = brief_date.strftime("%-d %B")

    # ── Battery ring ──
    # body_battery — пиковый заряд за день (Garmin API field `max`,
    # см. providers/garmin.py). Если None (cron не дотянул) — fallback
    # на вчерашний уровень. Если и его нет — None (UI покажет «—»).
    bb_today = garmin.get("body_battery") if garmin else None
    bb_today_int = bb_today if isinstance(bb_today, (int, float)) else None
    bb_yesterday_int = body_battery_yesterday if isinstance(body_battery_yesterday, (int, float)) else None

    if bb_today_int is not None:
        bb_int = bb_today_int
        battery_source_label = ""
    elif bb_yesterday_int is not None:
        bb_int = bb_yesterday_int
        yesterday_date_str = (brief_date - timedelta(days=1)).strftime("%-d %B")
        battery_source_label = f"по данным {yesterday_date_str}"
    else:
        bb_int = None
        battery_source_label = ""

    bb_color = _battery_color(bb_int)
    bb_deg = round((bb_int or 0) / 100 * 360)
    battery_value = str(int(bb_int)) if bb_int is not None else "—"
    battery_track = _battery_color_track(bb_color)

    # ── Hero-grid ──
    sleep_min = (garmin or {}).get("sleep_duration_min") or (helio or {}).get("sleep_duration_min")
    sleep_score_val = (garmin or {}).get("sleep_score") or (helio or {}).get("sleep_score")
    sleep_label = _minutes_to_label(sleep_min)
    sleep_score_str = str(sleep_score_val) if sleep_score_val is not None else "—"

    hrv_val = (garmin or {}).get("hrv") or (helio or {}).get("hrv_score")
    rhr_val = (garmin or {}).get("rhr") or (helio or {}).get("rhr")

    if hrv_baseline is None and hrv_7d:
        hrv_baseline = _hrv_baseline_from_7d(hrv_7d)
    hrv_status_str = _hrv_status(hrv_val, hrv_baseline)

    # ── Sleep pill ──
    sleep_pill_text, sleep_pill_class = _sleep_pill(sleep_score_val)

    # ── Sleep stages percentages + labels ──
    deep_pct_val = (garmin or {}).get("deep_sleep_pct") or (helio or {}).get("deep_sleep_pct")
    d_pct, r_pct, l_pct, a_pct = _build_sleep_pcts(sleep_min, deep_pct_val)

    # Реальные минуты (если есть deep_pct_val и общая длительность)
    if sleep_min and deep_pct_val:
        deep_min = round(sleep_min * deep_pct_val / 100)
        rem_min = round(sleep_min * 0.22)
        # Light + Awake делят остаток поровну если Awake задан
        awake_min = round(sleep_min * 0.03)
        light_min = max(0, sleep_min - deep_min - rem_min - awake_min)
    else:
        deep_min = rem_min = light_min = awake_min = None

    sleep_deep_label = _minutes_to_label(deep_min) if deep_min else "—"
    sleep_rem_label = _minutes_to_label(rem_min) if rem_min else "—"
    sleep_light_label = _minutes_to_label(light_min) if light_min else "—"
    sleep_awake_label = _minutes_to_label(awake_min) if awake_min else ""

    # Ось сна: выдумываем на основе типичного 23:00 → 07:00 (по концепту)
    sleep_start_label = "23:18"
    sleep_mid_label = "03:00"
    sleep_end_label = "07:00"

    # ── 2c SpO2 + HRV-trend ──
    # Приоритет: garmin_today.spo2 → helio_today.spo2 → garmin_yesterday.spo2 → helio_yesterday.spo2.
    # Если за сегодня null (cron не дотянул) — берём вчерашнее значение и помечаем.
    spo2_today = (garmin or {}).get("spo2") or (helio or {}).get("spo2")
    spo2_yest  = spo2_yesterday  # приходит из fetch_live_context

    if spo2_today is not None:
        spo2_val = spo2_today
        spo2_source_label = ""
    elif spo2_yest is not None:
        spo2_val = spo2_yest
        yesterday_date_str = (brief_date - timedelta(days=1)).strftime("%-d %B")
        spo2_source_label = f"по данным {yesterday_date_str}"
    else:
        spo2_val = None
        spo2_source_label = ""

    spo2_value = f"{spo2_val:.0f}" if spo2_val is not None else "—"
    # Подпись «стабильно всю ночь» показываем если spo2 ≥ 95 (по спеке v2)
    spo2_meta = "стабильно всю ночь" if spo2_val is not None and spo2_val >= 95 else "—"

    hrv_trend_path, hrv_trend_area = _hrv_trend_svg(hrv_7d or [])

    # ── 3b metrics ──
    delta = body_battery_delta
    delta_label = ""
    delta_class = "delta-good"
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        delta_label = f"{sign}{delta} vs вчера"
        delta_class = "delta-good" if delta >= 0 else "delta-rose"

    readiness_label, readiness_pill = _readiness_label_and_color(
        (garmin or {}).get("training_readiness") or (helio or {}).get("readiness")
    )
    readiness_meta = "Лучшее окно утром" if readiness_pill == "pill-blue" else "—"

    stress_label, stress_pill, stress_meta = _stress_label_and_color(
        stress_yesterday if stress_yesterday is not None
        else ((garmin or {}).get("stress") or (helio or {}).get("stress"))
    )

    # SpO2 квадрата — за вчера (по спеке v2), fallback на сегодня
    spo2_for_square = spo2_yesterday if spo2_yesterday is not None else spo2_val
    # Если есть вчерашний — переписываем value для квадрата
    if spo2_for_square is not None and spo2_yesterday is not None:
        spo2_value_square = f"{spo2_for_square:.0f}"
    else:
        spo2_value_square = spo2_value
    spo2_meta_short = "Ровное дыхание" if spo2_for_square is not None and spo2_for_square >= 95 else "—"

    # ── 4/1 Meetings + Deep Work ──
    agenda_raw = _build_deep_work_slots(
        calendar, body_battery=bb_int, sleep_score=sleep_score_val
    )
    meeting_count = len([e for e in calendar])
    deepwork_count = len([e for e in agenda_raw if e.get("_type") == "deepwork"])
    if meeting_count == 0:
        meeting_count_label = "Свободный день"
    elif deepwork_count > 0:
        meeting_count_label = f"{meeting_count} встреч + DW"
    else:
        meeting_count_label = f"{meeting_count} meetings"

    # ── 4/2 Tasks ──
    tasks_count = len(tasks)
    if tasks_count == 0:
        tasks_count_label = "Свободно"
    else:
        tasks_count_label = f"{tasks_count} tasks"

    # ── 5 Movement ──
    steps_val = (garmin or {}).get("totalSteps") or (helio or {}).get("steps")
    steps_goal = 10000
    steps_pct = min(100, _pct(steps_val, steps_goal))

    total_kcal = sum(e.get("kcal", 0) for e in food)
    kcal_burned_val = (
        ((garmin or {}).get("resting_kcal") or 0)
        + ((garmin or {}).get("active_kcal") or 0)
    )
    if not kcal_burned_val and helio:
        kcal_burned_val = helio.get("kcal") or 0

    kcal_burned_pct = min(100, _pct(kcal_burned_val, 2980))
    kcal_eaten_pct = min(100, _pct(total_kcal, 2980))

    # Баланс: + если профицит (съели > потратили), − если дефицит.
    # Это стандартная конвенция в питании.
    balance = total_kcal - kcal_burned_val
    sign_b = "+" if balance >= 0 else "−"
    if balance > 300:
        advice = "Профицит — активнее подвигайся в течение дня."
    elif balance < -300:
        advice = "Дефицит — стоит закрыть окно плотным ужином."
    elif abs(balance) <= 300:
        advice = "Неплохой день, чтобы не переусердствовать."
    else:
        advice = "Баланс в норме."
    balance_meta = f"Баланс {sign_b}{abs(balance)} ккал. {advice}"

    activity_pill = _activity_pill(steps_val, steps_goal, balance)

    # ── 6 Weather ──
    weather_periods_out = []
    max_wind = 0.0
    for w in weather:
        period = w.get("period", "")
        temp = w.get("temp")
        condition_key = (w.get("condition") or "").lower()
        icon = _WEATHER_EMOJI.get(condition_key, "☀️")
        wind = w.get("wind") or 0
        max_wind = max(max_wind, wind if isinstance(wind, (int, float)) else 0)

        weather_periods_out.append({
            "label": {"morning": "Утро", "day": "День", "evening": "Вечер"}.get(period, period),
            "icon": icon,
            "temp": f"{temp:.0f}°" if temp is not None else "—",
            "condition": w.get("condition") or "—",
        })

    if max_wind:
        kmh = round(max_wind * 3.6)
        if kmh >= 30:
            wind_pill_label = f"Wind {kmh} km/h"
        elif kmh >= 10:
            wind_pill_label = f"Wind {kmh} km/h"
        else:
            wind_pill_label = "Calm"
    else:
        wind_pill_label = ""

    weather_vs_yesterday = _build_weather_vs_yesterday(weather, weather_yesterday)

    return PlayfulContext(
        date_label=date_label,
        headline=narrative_headline,
        headline_summary=narrative_summary,
        battery_value=battery_value,
        battery_color=bb_color,
        battery_deg=bb_deg,
        battery_track=battery_track,
        battery_source_label=battery_source_label,
        sleep_label=sleep_label,
        sleep_score=sleep_score_str,
        hrv_value=str(hrv_val) if hrv_val is not None else "—",
        hrv_status=hrv_status_str,
        rhr_value=str(rhr_val) if rhr_val is not None else "—",
        sleep_deep_pct=d_pct,
        sleep_rem_pct=r_pct,
        sleep_light_pct=l_pct,
        sleep_awake_pct=a_pct,
        sleep_deep_label=sleep_deep_label,
        sleep_rem_label=sleep_rem_label,
        sleep_light_label=sleep_light_label,
        sleep_awake_label=sleep_awake_label,
        sleep_start_label=sleep_start_label,
        sleep_mid_label=sleep_mid_label,
        sleep_end_label=sleep_end_label,
        sleep_pill_class=sleep_pill_class,
        sleep_pill_text=sleep_pill_text,
        spo2_value=spo2_value,
        spo2_value_square=spo2_value_square,
        spo2_meta=spo2_meta,
        spo2_source_label=spo2_source_label,
        hrv_trend_path=hrv_trend_path,
        hrv_trend_area=hrv_trend_area,
        body_battery_delta=delta,
        body_battery_delta_label=delta_label,
        body_battery_delta_class=delta_class,
        readiness_label=readiness_label,
        readiness_meta=readiness_meta,
        stress_label=stress_label,
        stress_meta=stress_meta,
        spo2_meta_short=spo2_meta_short,
        focus_window_label=focus_window,
        meeting_count_label=meeting_count_label,
        agenda_items=agenda_raw,
        tasks=tasks,
        tasks_count_label=tasks_count_label,
        steps_value=_format_num(steps_val),
        steps_goal=steps_goal,
        steps_pct=steps_pct,
        kcal_burned=_format_num(kcal_burned_val),
        kcal_burned_pct=kcal_burned_pct,
        kcal_eaten=_format_num(total_kcal),
        kcal_eaten_pct=kcal_eaten_pct,
        balance_meta=balance_meta,
        activity_pill=activity_pill,
        wind_pill_label=wind_pill_label,
        weather_periods=weather_periods_out,
        weather_vs_yesterday=weather_vs_yesterday,
        footer_title=narrative_footer_title,
        footer_text=narrative_footer_text,
    )


def render_playful_html(ctx: PlayfulContext) -> str:
    template = _env.get_template("brief_playful.html.j2")
    return template.render(**ctx.__dict__)


# ─────────────────────────────────────────────────────────────────────
# Demo-фикстуры (для визуальной проверки без живого pipeline)
# ─────────────────────────────────────────────────────────────────────

DEMO_CONTEXT_DICT = {
    "brief_date": date(2026, 6, 27),
    "garmin": {
        "sleep_duration_min": 462,        # 7ч 42м
        "sleep_score": 92,
        "deep_sleep_pct": 21.0,           # 21% deep
        "hrv": 61,
        "rhr": 52,
        "spo2": 97,
        "body_battery": 92,
        "training_readiness": 84,
        "stress": 22,                     # вчерашний — Low
        "totalSteps": 8460,
        "resting_kcal": 1700,
        "active_kcal": 480,
    },
    "helio": None,                        # по спеке v2 Helio не используем
    "food": [
        {"meal_name": "завтрак", "kcal": 520, "protein": 30, "fat": 18, "carbs": 60},
        {"meal_name": "обед",     "kcal": 820, "protein": 50, "fat": 30, "carbs": 80},
        {"meal_name": "перекус",  "kcal": 280, "protein": 12, "fat": 10, "carbs": 30},
        {"meal_name": "ужин",     "kcal": 340, "protein": 25, "fat": 12, "carbs": 28},
    ],
    "weather": [
        {"period": "morning", "temp": 18, "condition": "Clear",  "wind": 2.0},
        {"period": "day",     "temp": 25, "condition": "Clouds", "wind": 3.5},
        {"period": "evening", "temp": 20, "condition": "Wind",   "wind": 4.0},
    ],
    "weather_yesterday": [
        {"period": "morning", "temp": 15, "condition": "Clear",  "wind": 1.5},
        {"period": "day",     "temp": 20, "condition": "Clear",  "wind": 2.5},
        {"period": "evening", "temp": 17, "condition": "Clouds", "wind": 1.0},
    ],
    "calendar": [
        {"title": "Product sync",   "start_time": "09:30", "duration_minutes": 30},
        {"title": "1:1",            "start_time": "13:00", "duration_minutes": 45},
        {"title": "Client call",    "start_time": "16:30", "duration_minutes": 60},
    ],
    "tasks": [
        {"title": "Подготовить презентацию Q3", "priority": 1, "due_time": "12:00"},
        {"title": "Ответить на письма",          "priority": 2, "due_time": ""},
        {"title": "Согласовать ТЗ с дизайнером", "priority": 3, "due_time": "15:00"},
        {"title": "Забронировать переговорку",   "priority": 4, "due_time": ""},
    ],
    "hrv_7d": [58, 60, 59, 62, 63, 60, 61],
    "body_battery_delta": 12,
    "narrative_headline": "Солнышко, ресурс сегодня на месте",
    "narrative_summary": (
        "Тот же теплый и живой характер, но уже в более взрослой продуктовой форме: "
        "утро сильное, фокус высокий, а вечер лучше оставить мягким."
    ),
    "narrative_footer_title": "Не расплескать хорошее утро",
    "narrative_footer_text": (
        "Утро сегодня очень вкусное по ресурсу. Лучше не отдавать его мелочам: закрыть "
        "серьезное до обеда, после 15:00 не насиловать внимание и вечером добрать движение "
        "чем-то приятным."
    ),
    "focus_window": "Focus 08:30–11:30",
}


def render_demo(out_path: str | Path) -> Path:
    """Сгенерить demo-HTML с фикстурами и сохранить в out_path."""
    out = Path(out_path)
    ctx = build_playful_context(**DEMO_CONTEXT_DICT)
    html = render_playful_html(ctx)
    out.write_text(html, encoding="utf-8")
    return out


def render_from_json(json_path: str | Path, out_path: str | Path) -> Path:
    """Сгенерить HTML из JSON-файла с тем же форматом, что DEMO_CONTEXT_DICT."""
    out = Path(out_path)
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    # brief_date как строка → date
    if isinstance(data.get("brief_date"), str):
        data["brief_date"] = datetime.fromisoformat(data["brief_date"]).date()
    ctx = build_playful_context(**data)
    html = render_playful_html(ctx)
    out.write_text(html, encoding="utf-8")
    return out


# ─────────────────────────────────────────────────────────────────────
# Live-режим: подтягивает данные из Supabase morning_brief_v2
# ─────────────────────────────────────────────────────────────────────

def _load_dotenv(env_path: Path | None = None) -> None:
    """Минимальный .env loader — кладёт пары в os.environ если их там нет."""
    if env_path is None:
        # Ищем .env в /root/morning_brief_v2/, /root/, и вверх до 3 уровней
        candidates = [
            Path("/root/morning_brief_v2/.env"),
            Path("/root/.env"),
            Path.cwd() / ".env",
        ]
        for c in candidates:
            if c.exists():
                env_path = c
                break
    if not env_path or not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _map_garmin_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Supabase garmin_metrics → формат build_playful_context().

    Поля в схеме (см. db/client.py + VaulTec/Ревью утреннего брифа гаджеты.md):
      body_battery, hrv, rhr, spo2, training_readiness, stress,
      sleep_duration_min, sleep_score, deep_sleep_pct,
      resting_kcal, active_kcal, total_kcal, total_steps
    """
    if not row:
        return None
    # Маппинг total_kcal/total_steps не всегда есть в строке —
    # вычисляем из resting_kcal + active_kcal если нет
    resting = row.get("resting_kcal") or 0
    active = row.get("active_kcal") or 0
    total_kcal = row.get("total_kcal") or (resting + active)
    return {
        "sleep_duration_min": row.get("sleep_duration_min"),
        "sleep_score": row.get("sleep_score"),
        "deep_sleep_pct": row.get("deep_sleep_pct"),
        "hrv": row.get("hrv"),
        "body_battery": row.get("body_battery"),
        "rhr": row.get("rhr"),
        "spo2": row.get("spo2"),
        "training_readiness": row.get("training_readiness"),
        "stress": row.get("stress"),
        "totalSteps": row.get("total_steps"),
        "resting_kcal": resting,
        "active_kcal": active,
        "_kcal_total": total_kcal,
    }


def _map_weather_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """weather_log → list of {period, temp, condition, wind}.

    Колонки (по схеме): period, temp, condition, wind_speed, ...
    """
    out = []
    for r in rows:
        out.append({
            "period": r.get("period") or "",
            "temp": r.get("temp"),
            "condition": r.get("condition"),
            "wind": r.get("wind_speed") or r.get("wind"),
        })
    return out


def _map_food_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """food_log → list of {meal_name, kcal, protein, fat, carbs}."""
    return [
        {
            "meal_name": r.get("meal_name") or "",
            "kcal": r.get("kcal") or 0,
            "protein": r.get("protein") or 0,
            "fat": r.get("fat") or 0,
            "carbs": r.get("carbs") or 0,
        }
        for r in rows
    ]


def _map_calendar_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """calendar_events → list of {title, start_time, duration_minutes}.

    start_time в БД — TIMETZ (HH:MM:SS+TZ). build_playful_context ждёт HH:MM,
    поэтому обрезаем.
    """
    out = []
    for r in rows:
        st = r.get("start_time") or ""
        # '11:00:00+01:00' → '11:00'
        if "T" in st:
            st = st.split("T", 1)[1]
        short_time = st[:5] if len(st) >= 5 else st
        out.append({
            "title": r.get("title") or "",
            "start_time": short_time or None,
            "duration_minutes": r.get("duration_minutes"),
        })
    return out


def _map_task_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tasks → list of {title, priority, due_time}.

    В схеме есть только title+priority; due_time вычисляем из date если есть,
    иначе пусто (отобразится без времени).
    """
    return [
        {
            "title": r.get("title") or "",
            "priority": r.get("priority") or 4,
            "due_time": "",  # в схеме нет отдельной колонки due_time
        }
        for r in rows
    ]


def fetch_live_context(brief_date: date) -> dict[str, Any]:
    """Подтянуть данные за brief_date из Supabase.

    Returns: dict, готовый для build_playful_context(**data).
    Raises: RuntimeError если SUPABASE_URL/KEY не заданы.
    """
    _load_dotenv()
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
        raise RuntimeError(
            "SUPABASE_URL/SUPABASE_KEY не найдены. "
            "Проверь /root/morning_brief_v2/.env."
        )

    # Lazy import — db/client.py требует supabase, не подтягиваем в demo-режиме
    from db.client import (
        get_garmin_metrics,
        get_helio_metrics,
        get_food_log,
        get_weather_log,
        get_calendar_events,
        get_tasks,
    )

    garmin_row = get_garmin_metrics(brief_date)
    helio_row = get_helio_metrics(brief_date)

    # food_date = brief_date - 1 (по правилам брифа)
    food_date = brief_date - timedelta(days=1)
    weather_yesterday_date = food_date
    # Вчерашние данные для Stress / SpO2 / Body Battery delta (по спеке v2)
    yesterday_date = food_date

    food_rows = get_food_log(food_date)
    weather_rows = get_weather_log(brief_date)
    weather_yesterday_rows = (
        get_weather_log(weather_yesterday_date) if weather_yesterday_date else []
    )
    calendar_rows = get_calendar_events(brief_date)
    task_rows = get_tasks(brief_date)

    # Вчерашние строки для Stress / SpO2 (по спеке v2 «Stress за вчера»)
    garmin_yesterday = get_garmin_metrics(yesterday_date)
    helio_yesterday = get_helio_metrics(yesterday_date)

    # Body Battery delta: (сегодня - вчера). Приоритет: garmin > helio (только garmin).
    bb_today = (garmin_row or {}).get("body_battery") if garmin_row else None
    bb_yesterday = (garmin_yesterday or {}).get("body_battery") if garmin_yesterday else None
    body_battery_delta = (
        bb_today - bb_yesterday
        if (bb_today is not None and bb_yesterday is not None)
        else None
    )
    # Body Battery вчера — для fallback в ring когда cron за сегодня не отработал
    body_battery_yesterday = bb_yesterday

    # Stress / SpO2 за вчера: garmin приоритет, fallback helio
    stress_yesterday = (
        ((garmin_yesterday or {}).get("stress"))
        or ((helio_yesterday or {}).get("stress"))
    )
    spo2_yesterday = (
        ((garmin_yesterday or {}).get("spo2"))
        or ((helio_yesterday or {}).get("spo2"))
    )

    return {
        "brief_date": brief_date,
        "garmin": _map_garmin_row(garmin_row),
        "helio": helio_row,
        "food": _map_food_rows(food_rows),
        "weather": _map_weather_rows(weather_rows),
        "weather_yesterday": _map_weather_rows(weather_yesterday_rows),
        "calendar": _map_calendar_rows(calendar_rows),
        "tasks": _map_task_rows(task_rows),
        # Числа для 3b — берутся за вчера по спеке v2
        "stress_yesterday": stress_yesterday,
        "spo2_yesterday": spo2_yesterday,
        "body_battery_delta": body_battery_delta,
        "body_battery_yesterday": body_battery_yesterday,
        "narrative_headline": f"Утро {brief_date.strftime('%-d %B')}",
        "narrative_summary": (
            "Утренний бриф собран из живых данных. "
            "Нарратив-NLG не подключен — текст ниже дефолтный."
        ),
        "narrative_footer_title": "Хорошее начало",
        "narrative_footer_text": (
            "Бриф собран. Проверь ресурс утром и не отдавай сильное утро мелочам."
        ),
        "focus_window": "Focus 08:30–11:30",
    }


def render_live(brief_date: date, out_path: str | Path) -> Path:
    """Live-рендер из Supabase за указанную дату."""
    out = Path(out_path)
    data = fetch_live_context(brief_date)
    ctx = build_playful_context(**data)
    html = render_playful_html(ctx)
    out.write_text(html, encoding="utf-8")
    return out


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Render Playful morning brief HTML")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="Use demo fixtures")
    group.add_argument("--from-json", type=str, help="Render from JSON context file")
    group.add_argument("--live", action="store_true", help="Render from live morning_brief_v2 DB")
    parser.add_argument("--date", type=str, help="Brief date for --live (YYYY-MM-DD)")
    parser.add_argument("--out", type=str, default="/tmp/brief_playful.html", help="Output HTML path")
    args = parser.parse_args()

    if args.demo:
        out = render_demo(args.out)
        print(f"[demo] rendered → {out}")
    elif args.from_json:
        out = render_from_json(args.from_json, args.out)
        print(f"[json] rendered → {out}")
    elif args.live:
        if not args.date:
            print("ERROR: --live требует --date YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)
        try:
            target = datetime.fromisoformat(args.date).date()
        except ValueError:
            print(f"ERROR: неверная дата: {args.date!r} (ожидаю YYYY-MM-DD)", file=sys.stderr)
            sys.exit(2)
        try:
            out = render_live(target, args.out)
            print(f"[live] rendered → {out}")
        except Exception as e:
            print(f"ERROR [live]: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()