# `pult/` — VPS-side worker

> **For operator:** Как рестартить воркер, как смотреть логи, как поменять секрет.

## Файлы

```
pult/
├── schema.sql              — таблица jobs + 2 RPC (claim_next_job, reap_stuck_jobs)
├── worker.py               — главный loop (claim → run → status)
├── worker_test.sh          — smoke-тест через ручной INSERT
└── README.md               — этот файл
```

## Команды

```bash
# Статус
systemctl status mbrief-pult-worker

# Логи (следить в реальном времени)
journalctl -u mbrief-pult-worker -f

# Последние 50 строк
journalctl -u mbrief-pult-worker -n 50 --no-pager

# Рестарт
systemctl restart mbrief-pult-worker

# Остановить (jobs накопятся в pending)
systemctl stop mbrief-pult-worker

# Включить обратно
systemctl start mbrief-pult-worker

# Отключить автозапуск (но не выключать прямо сейчас)
systemctl disable mbrief-pult-worker
```

## Конфигурация

Воркер читает `/root/morning_brief_v2/.env`. Нужны:
- `SUPABASE_URL`
- `SUPABASE_KEY` (anon, не service_role)

`SUPABASE_SERVICE_ROLE_KEY` НЕ нужен воркеру — он использует anon key +
SECURITY DEFINER RPC.

## Grants на таблицу (Supabase → SQL editor)

`pult/schema.sql` создаёт таблицу, но **выдаёт grants только ролям
`authenticator` и `service_role`**. Этого достаточно для Vercel Function
с service_role JWT.

Если grant'ов нет — Vercel Function получает `42501 permission denied`
при INSERT (это происходит потому что PostgREST подключается как
`authenticator`, и grants проверяются для login role, а не для
current_user из JWT claim).

Примените `pult/schema.sql` целиком — grants уже включены в конец файла.

Если таблица уже создана и grant'ов не хватает, выполните:

```sql
grant insert, update, delete, select on morning_brief_v2.jobs to authenticator, service_role;
grant usage, select on sequence morning_brief_v2.jobs_id_seq to authenticator, service_role;
NOTIFY pgrst, 'reload schema';
```

**Диагностика:** `https://rus-morning-brief.vercel.app/api/diag?key=<PULT_SHARED_SECRET>`
показывает `has_table_insert_for_*` для каждой роли.

## Что делает RPC

### `claim_next_job()`

`SELECT * FROM jobs WHERE status='pending' ORDER BY triggered_at ASC
FOR UPDATE SKIP LOCKED LIMIT 1`. Атомарно. Если несколько воркеров
запущено — каждый получит свой job.

### `reap_stuck_jobs()`

`UPDATE jobs SET status='orphaned' WHERE status='running' AND
triggered_at < now() - interval '15 minutes'`. Возвращает количество.

## Таймауты скриптов

| Скрипт | Timeout (сек) | Источник |
|---|---|---|
| `garmin-yesterday` / `garmin-today` | 60 | быстро, < 15 сек обычно |
| `weather`, `calendar`, `todoist`, `food` | 60 | обычно 3-10 сек |
| `llm` | 180 | §7 — 45s timeout на `hermes -z` + retry |
| `render-publish` | 600 | §29 — archive+publish бывает 150-300s |

Поменять в `worker.py` → `TIMEOUTS` dict.

## Добавить новый скрипт

1. Положить bash-скрипт в `scripts/manual/<name>.sh` (chmod +x).
2. В `pult/worker.py` → `SCRIPTS` добавить `'name': ['bash', '/path/to/script.sh']`.
3. В `TIMEOUTS` добавить `'name': <seconds>`.
4. В `api/trigger.js` → `ALLOWED_SCRIPTS` добавить `'name'`.
5. В `web/pult/pult.js` → `BUTTONS` добавить `{id, label, script}`.
6. Commit + push → Vercel auto-deploy. Воркер подхватит без рестарта.

## Если reaper ставит orphaned слишком часто

Увеличить `REAPER_THRESHOLD_SEC` в `worker.py` (по умолчанию 900 = 15 мин).

## Если claim возвращает 0 rows постоянно

- `SELECT COUNT(*) FROM morning_brief_v2.jobs WHERE status='pending'`
  — если 0, то pending нет, worker ждёт правильно.
- Если есть pending — проверить `journalctl -u mbrief-pult-worker` на
  ошибки (например, `relation does not exist`).
