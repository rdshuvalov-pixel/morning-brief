-- Миграция 007b — RLS policy для brief_block_narratives.
--
-- Контекст: 007 создал таблицу, ты добавил RLS, но без явных policies
-- anon-роль получает 42501 (permission denied). Этот фикс добавляет
-- policy для anon (anon-ключ используется и VPS Python, и Vercel Function).
--
-- ВАЖНО: id = UUID DEFAULT gen_random_uuid(), sequence нет.
-- Если у тебя в проекте имена policy другие или ты хочешь более строгую
-- политику (например, только service_role может писать) — скажи, переделаю.

BEGIN;

SET search_path TO morning_brief_v2;

-- Включаем RLS явно (если ты не включил — ALTER TABLE пропустит ошибку)
ALTER TABLE brief_block_narratives ENABLE ROW LEVEL SECURITY;

-- Удаляем старую policy на случай повторного применения
DROP POLICY IF EXISTS "anon_full_access" ON brief_block_narratives;

-- Policy: anon-роль видит и меняет все строки
CREATE POLICY "anon_full_access" ON brief_block_narratives
    FOR ALL
    TO anon
    USING (true)
    WITH CHECK (true);

-- Grants на саму таблицу (anon-роль по умолчанию не имеет прав на новую таблицу)
GRANT ALL ON brief_block_narratives TO anon;

COMMIT;