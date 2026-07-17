-- Миграция 008 — Per-block narratives как 5 колонок в briefs.
--
-- ОТКАЗ от решения из 007 (отдельная таблица). Причины:
--   - YAGNI: индекс по block_name "для будущих запросов" не нужен.
--   - Read в render_playful.py становится тривиальным: brief_row.get('narrative_weather').
--   - UPSERT на 1 блок: briefs.UPDATE WHERE id=X (атомарно).
--   - Mental model: бриф — это документ с полями, а не join 5 таблиц.
--   - Все остальные narrative-поля (headline, lead, footer_*, opinions) уже в briefs.
--     Per-block narrative логически лежит рядом с ними.
--
-- ПОРЯДОК ОПЕРАЦИЙ (атомарно, в одной транзакции):
--   1. ALTER TABLE briefs ADD COLUMN ... — создаём пустые колонки (NULL).
--      Нужно ДО UPDATE, иначе 42703 column does not exist.
--   2. UPDATE briefs SET ... — заполняем колонки из старой таблицы.
--      COALESCE не перезаписывает уже заполненное (защита от повторного запуска).
--   3. DROP TABLE brief_block_narratives — удаляем старую таблицу.
--   4. UPDATE briefs SET narrative_blocks_meta — заполняем meta (после ALTER).
--
-- ВАЖНО: SET search_path TO morning_brief_v2 — иначе 42P01.
-- ВАЖНО: TEXT-колонки nullable — перегенерация поэтапная (не все 5 сразу).

BEGIN;

SET search_path TO morning_brief_v2;

-- ─── 1. Создаём 5 текстовых колонок + meta (если ещё не созданы) ─────────
--    IF NOT EXISTS — идемпотентность, если запустишь миграцию дважды.
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_weather    TEXT;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_tasks      TEXT;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_movement   TEXT;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_calendar   TEXT;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_battery    TEXT;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS narrative_blocks_meta JSONB;

-- ─── 2. ПЕРЕНОС: 5 строк из brief_block_narratives → 5 столбцов в briefs ──
--    COALESCE не перезаписывает уже заполненное — защита от повторного запуска.
UPDATE briefs b SET
    narrative_weather  = COALESCE(b.narrative_weather,
        (SELECT text FROM brief_block_narratives bbn
         WHERE bbn.brief_id = b.id AND bbn.block_name = 'weather')),
    narrative_tasks    = COALESCE(b.narrative_tasks,
        (SELECT text FROM brief_block_narratives bbn
         WHERE bbn.brief_id = b.id AND bbn.block_name = 'tasks')),
    narrative_movement = COALESCE(b.narrative_movement,
        (SELECT text FROM brief_block_narratives bbn
         WHERE bbn.brief_id = b.id AND bbn.block_name = 'movement')),
    narrative_calendar = COALESCE(b.narrative_calendar,
        (SELECT text FROM brief_block_narratives bbn
         WHERE bbn.brief_id = b.id AND bbn.block_name = 'calendar')),
    narrative_battery  = COALESCE(b.narrative_battery,
        (SELECT text FROM brief_block_narratives bbn
         WHERE bbn.brief_id = b.id AND bbn.block_name = 'battery'))
WHERE EXISTS (SELECT 1 FROM brief_block_narratives bbn WHERE bbn.brief_id = b.id);

-- ─── 3. Удаляем старую таблицу ───────────────────────────────────────────
DROP TABLE IF EXISTS brief_block_narratives CASCADE;

-- ─── 4. Заполняем narrative_blocks_meta (только для briefs с данными) ───
UPDATE briefs
SET narrative_blocks_meta = jsonb_build_object(
    'weather',  jsonb_build_object('source', 'migrated-from-007', 'chars', length(narrative_weather)),
    'tasks',    jsonb_build_object('source', 'migrated-from-007', 'chars', length(narrative_tasks)),
    'movement', jsonb_build_object('source', 'migrated-from-007', 'chars', length(narrative_movement)),
    'calendar', jsonb_build_object('source', 'migrated-from-007', 'chars', length(narrative_calendar)),
    'battery',  jsonb_build_object('source', 'migrated-from-007', 'chars', length(narrative_battery))
)
WHERE narrative_weather IS NOT NULL
   OR narrative_tasks   IS NOT NULL
   OR narrative_movement IS NOT NULL
   OR narrative_calendar IS NOT NULL
   OR narrative_battery  IS NOT NULL;

COMMIT;