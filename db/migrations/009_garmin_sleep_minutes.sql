-- Morning Brief v2 — Garmin sleep minute-by-minute time-series.
--
-- Контекст: Garmin Connect direct API возвращает поминутные ряды во сне:
--   sleepMovement              (591 точка, 1-мин интервалы, поле activityLevel)
--   wellnessEpochSPO2DataDTOList (411 точек, ~3-мин SpO2)
--   hrvData                    (94 точки, ~5-мин HRV)
--   sleepStress                (157 точек, ~2-мин stress)
--   sleepBodyBattery           (157 точек, ~2-мин BB)
--   wellnessEpochRespirationDataDTOList (236 точек, ~1-мин respiration)
--   sleepLevels                (20 окон стадий с activityLevel 0..3)
--
-- Текущий providers/garmin.py тащит только агрегаты (sleep_duration_min,
-- sleep_score, hrv lastNightAvg, body_battery peak). Поминутные данные
-- отбрасываются и в morning_brief_v2 не попадают.
--
-- Эта таблица фиксирует поминутную развертку сна — для последующего
-- отображения в брифе (time-series: SpO2/HRV/BB по минутам) и анализа.
--
-- Схема:
--   date        — календарная дата (UTC), привязана к minute_ts::date
--   minute_ts   — начало минуты (UTC). UNIQUE с date → идемпотентный upsert.
--   stage       — стадия сна в эту минуту:
--                   0 = rem, 1 = light, 2 = deep, 3 = awake, NULL = вне окна сна
--                 (Garmin activityLevel: 0=awake, 1=light, 2=deep, 3=rem)
--   movement    — activityLevel в эту минуту (float, NULL вне окна сна)
--   spo2        — SpO2 % (smallint), nullable (прибор может не измерять)
--   hrv         — HRV ms (smallint), nullable (прибор ~5-мин интервал)
--   stress      — stress 0..100 (smallint), nullable
--   body_battery — BB 0..100 (smallint), nullable
--   respiration — дыхание (float), nullable
--
-- IMPORTANT: Таблица в schema morning_brief_v2 (не public).
-- Bare `CREATE TABLE garmin_sleep_minutes` упадёт с 42P01 в Supabase SQL Editor.
--
-- Apply через Supabase SQL Editor на проекте dkmoocytmhzxhjzmodmj:
--   SET search_path TO morning_brief_v2;
--   -- затем содержимое файла ниже
BEGIN;

SET search_path TO morning_brief_v2;

CREATE TABLE IF NOT EXISTS garmin_sleep_minutes (
    date         DATE        NOT NULL,
    minute_ts    TIMESTAMPTZ NOT NULL,
    stage        SMALLINT,         -- 0=rem, 1=light, 2=deep, 3=awake, NULL=вне сна
    movement     NUMERIC(7,3),     -- activityLevel, float
    spo2         SMALLINT,         -- %
    hrv          SMALLINT,         -- ms
    stress       SMALLINT,         -- 0..100
    body_battery SMALLINT,         -- 0..100
    respiration  NUMERIC(5,2),     -- breaths/min
    CONSTRAINT garmin_sleep_minutes_date_minute_ts_unique UNIQUE (date, minute_ts)
);

CREATE INDEX IF NOT EXISTS idx_garmin_sleep_minutes_date
    ON garmin_sleep_minutes(date);

-- RLS + grants (anon-ключ используется и VPS Python, и Vercel Function;
-- см. миграцию 007b — без явных policies получим 42501 permission denied)
ALTER TABLE garmin_sleep_minutes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_full_access" ON garmin_sleep_minutes;

CREATE POLICY "anon_full_access" ON garmin_sleep_minutes
    FOR ALL
    TO anon
    USING (true)
    WITH CHECK (true);

GRANT ALL ON garmin_sleep_minutes TO anon;

COMMIT;
