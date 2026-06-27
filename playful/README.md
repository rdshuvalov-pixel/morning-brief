# playful/

Production Playful рендер утреннего брифа — отдельный пакет поверх существующего
`/root/morning_brief_v2/` pipeline.

## Что это

Jinja2-шаблон + Python-рендер, который собирает **мобильный** утренний бриф
(430px viewport, iPhone-размер) по v2-спеку: Hero с батарейкой, Sleep stages,
SpO2/HRV, статусная сетка, план дня с Deep Work, задачи Todoist, прогресс-бары
движения/питания, погода с vs-вчера, footer-сводка.

## Не ломает существующий pipeline

- Не трогает `renderer.py`, `narrator.py`, `brief_builder.py`
- Не зависит от LLM-нарратора (заголовки дефолтные)
- Использует существующий `db/client.py` для live-режима

## Использование

### Demo (фикстуры в коде)

```bash
cd /root/morning_brief_v2
./.venv/bin/python -m playful.render_playful --demo --out /tmp/brief.html
```

### Live (из Supabase morning_brief_v2)

```bash
./.venv/bin/python -m playful.render_playful --live --date 2026-06-27 --out /tmp/brief.html
```

Читает за `--date`:
- `garmin_metrics` → battery ring, sleep, HRV, RHR, SpO2, шаги, калории
- `helio_metrics` → fallback для HRV/RHR (если garmin пуст)
- `food_log` (за date − 1 день) → калории
- `weather_log` (сегодня и вчера) → температура, ветер, vs вчера
- `calendar_events` → meetings + Deep Work слот
- `tasks` → Todoist задачи (top-5)

`.env` нужен с `SUPABASE_URL` и `SUPABASE_KEY`.

### Из JSON

```bash
./.venv/bin/python -m playful.render_playful --from-json context.json --out /tmp/brief.html
```

Формат JSON совпадает с `playful.render_playful.DEMO_CONTEXT_DICT`.

## Скриншот

```bash
# Сначала отрендерить HTML (любой режим), потом:
playwright install chromium   # один раз
python /root/screenshot_brief.py
# → /tmp/brief_playful_screenshot.png
```

## Что внутри

- `playful/brief_playful.html.j2` — Jinja2-шаблон, 776 строк
- `playful/render_playful.py` — рендер с `build_playful_context()`, 800+ строк
- `playful/__init__.py` — пустой, маркер пакета

## Что НЕ подключено (TODO)

- NLG-нарратив (headline/summary/footer сейчас дефолтные строки)
- HRV-trend за 7 дней (sparkline рисуется, данных нет)
- Body Battery delta `+12 vs вчера` (нужен запрос за `date − 1`)
- Кастомные emoji для weather conditions