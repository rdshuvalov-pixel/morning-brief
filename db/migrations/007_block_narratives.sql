-- Morning Brief v2 — Per-block narratives.
--
-- Архитектурное решение (по согласованию с оператором 2026-07-17):
-- ВМЕСТО JSONB-колонок в briefs (narrative_blocks, narrative_blocks_meta)
-- делаем ОТДЕЛЬНУЮ ТАБЛИЦУ brief_block_narratives. Каждая строка — один
-- (brief_id, block_name) tuple. Это даёт:
--   - индексируемость по (brief_id, block_name) — UPSERT ON CONFLICT
--   - независимую перегенерацию одного блока без перетирания остальных
--   - историю изменений (ts + model фиксируется на каждую запись)
--   - отсутствие schema-cache проблем PGRST204 (новая таблица = свежий кэш)
--
-- Block names: weather | tasks | movement | calendar | battery
-- Source:      llm | fallback | empty
--
-- В Telegram НЕ идёт (там используется briefs.telegram_text + opinions).
-- Per-block narrative живёт ТОЛЬКО в браузерном HTML (rus-morning-brief.vercel.app)
-- и попадает туда через briefs.narrative_blocks JSONB view-column (генерируется
-- на лету в render_playful.py через JOIN, см. README narrative_blocks).
--
-- IMPORTANT: Table lives in `morning_brief_v2` schema (not `public`).
-- Apply via Supabase SQL Editor with search_path set.

BEGIN;

SET search_path TO morning_brief_v2;

CREATE TABLE IF NOT EXISTS brief_block_narratives (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id        UUID NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
    block_name      TEXT NOT NULL CHECK (block_name IN ('weather','tasks','movement','calendar','battery')),
    text            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'llm' CHECK (source IN ('llm','fallback','empty')),
    model           TEXT,
    chars           INT,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (brief_id, block_name)
);

CREATE INDEX IF NOT EXISTS idx_brief_block_narratives_brief_id ON brief_block_narratives(brief_id);
CREATE INDEX IF NOT EXISTS idx_brief_block_narratives_block    ON brief_block_narratives(block_name);

COMMIT;