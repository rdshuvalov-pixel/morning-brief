# `/pult` — кнопочный пульт утреннего брифа

> **For user:** Как зайти, как добавить 9-ю кнопку, troubleshooting.

## Что это

Страница `https://rus-morning-brief.vercel.app/pult` с 8 кнопками для ручного
запуска 8 шагов утреннего брифа на VPS. Каждая кнопка:

1. Ставит задачу в Supabase-таблицу `morning_brief_v2.jobs`.
2. Воркер на VPS (`mbrief-pult-worker.service`) забирает задачу в течение 10 сек.
3. Запускает соответствующий bash-скрипт из `scripts/manual/`.
4. Обновляет статус (`done` / `failed` / `orphaned`).
5. UI polling-ом показывает результат.

## Как зайти

```
https://rus-morning-brief.vercel.app/pult?key=<PULT_SHARED_SECRET>
```

`<PULT_SHARED_SECRET>` — это значение переменной `PULT_SHARED_SECRET` в
Vercel Dashboard → Settings → Environment Variables. Сгенерировать:
```bash
openssl rand -hex 16
```

После первого входа ключ сохранится в `sessionStorage` браузера — на этой
вкладке больше вводить не нужно. Но при новой вкладке / новом окне —
придётся ввести заново (это by design, не баг).

## Что делает каждая кнопка

| Кнопка | Скрипт | Pitfall |
|---|---|---|
| Garmin (вчера) | `scripts/manual/fetch_garmin_yesterday.sh` | Закрытые settled-поля |
| Garmin (сегодня) | `scripts/manual/fetch_garmin_today.sh` | §24a — пишет ВСЕ поля включая morning settled |
| Погода | `scripts/manual/fetch_weather.sh` | §7 — forecast отдаёт за сегодня/завтра |
| Календарь | `scripts/manual/fetch_calendar.sh` | §7 — hard-codes `today` |
| Задачи (Todoist) | `scripts/manual/fetch_todoist.sh` | §28 — non-deterministic count |
| Еда | `scripts/manual/fetch_food.sh` | §12b — `date = today - 1` в `food_log` |
| LLM-нарратив | `scripts/manual/generate_llm.sh` → `generate_llm.py --write` | §36 — verify→dry-run→write gate, 180s timeout |
| Render + publish | `scripts/manual/archive_and_publish.sh` | §29 — 150-300s; §36a — push с main (не feature branch) |

## Статусы

- **idle** — кнопка серая, можно нажимать.
- **running** — жёлтая пульсация, воркер выполняет скрипт.
- **done** — зелёная вспышка 5 сек, потом возвращается в idle.
- **failed** — красная вспышка 8 сек, в баннере показана причина (stderr обрезанный до 200 chars).
- **orphaned** — красная, если воркер не закончил job за 15 минут (reaper).

## Защита от двойного клика

Двойной клик защищён **двумя слоями** (belt and suspenders):
1. **UI block 3 сек** — кнопка disabled после первого клика.
2. **DB unique index** — `jobs_dedup_idx` на `(script, payload->>date) WHERE
   status IN ('pending','running')`. Второй INSERT вернёт `23505
   unique_violation` → Vercel Function возвращает существующий job_id.

## Troubleshooting

### Кнопка нажата, но статус навсегда pending

Значит воркер на VPS не запущен или не видит job'ы.
```bash
systemctl status mbrief-pult-worker
journalctl -u mbrief-pult-worker -n 30
```

### Кнопка даёт 401

Неверный или отсутствующий `?key=`. Введите ключ в поле ввода сверху.

### Кнопка даёт 401 даже с ключом

Vercel env `PULT_SHARED_SECRET` не установлен или отличается от того, что
вы вводите. Проверьте в Vercel Dashboard → Settings → Environment Variables.

### Worker пишет "claim error: relation does not exist"

Таблица `morning_brief_v2.jobs` не создана. Выполните `pult/schema.sql` в
Supabase SQL editor.

### Worker упал (status=orphaned через 15 мин)

Скрипт выполнялся дольше timeout. Можно:
- Подождать и кликнуть заново.
- Проверить `journalctl -u mbrief-pult-worker` на ошибки.

## Как добавить 9-ю кнопку

1. **Backend worker** — `pult/worker.py` → `SCRIPTS` dict и `TIMEOUTS`
   dict: добавить новую запись.
2. **Vercel Function** — `api/trigger.js` → `ALLOWED_SCRIPTS` array:
   добавить имя.
3. **Frontend** — `web/pult/pult.js` → `BUTTONS` array: добавить
   `{id, label, script}`.
4. Закоммитить и запушить → Vercel auto-deploy → воркер подхватит без
   перезапуска (новые скрипты читаются из `SCRIPTS` на каждой итерации).
