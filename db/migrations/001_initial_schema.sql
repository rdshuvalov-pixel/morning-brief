-- Morning Brief v2 — Initial Schema
BEGIN;

CREATE TABLE IF NOT EXISTS briefs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date        DATE UNIQUE NOT NULL,
    brief_url   TEXT,
    telegram_text TEXT,
    status      TEXT DEFAULT 'pending',
    collected_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS garmin_metrics (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id             UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date                 DATE UNIQUE NOT NULL,
    sleep_duration_min   INT,
    sleep_score          INT,
    deep_sleep_pct       NUMERIC(5,2),
    hrv                  INT,
    body_battery         INT,
    rhr                  INT,
    spo2                 NUMERIC(5,2),
    training_readiness   INT,
    stress               INT,
    skin_temp            NUMERIC(5,2)
);

CREATE TABLE IF NOT EXISTS helio_metrics (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id     UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date         DATE UNIQUE NOT NULL,
    readiness    INT,
    physical     INT,
    mental       INT,
    hrv_score    INT,
    sleep_hrv    INT,
    rhr          INT,
    steps        INT,
    kcal         INT
);

CREATE TABLE IF NOT EXISTS food_log (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date     DATE NOT NULL,
    meal_name   TEXT NOT NULL,
    kcal     INT,
    protein  NUMERIC(6,1),
    fat      NUMERIC(6,1),
    carbs    NUMERIC(6,1)
);

CREATE TABLE IF NOT EXISTS weather_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id   UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date       DATE NOT NULL,
    period     TEXT NOT NULL,
    temp       NUMERIC(4,1),
    condition  TEXT,
    wind       NUMERIC(4,1)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id        UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    title           TEXT NOT NULL,
    start_time      TIMETZ,
    duration_minutes INT
);

CREATE TABLE IF NOT EXISTS tasks (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id UUID REFERENCES briefs(id) ON DELETE CASCADE,
    date     DATE NOT NULL,
    title    TEXT NOT NULL,
    priority INT
);

CREATE INDEX IF NOT EXISTS idx_garmin_brief_id  ON garmin_metrics(brief_id);
CREATE INDEX IF NOT EXISTS idx_helio_brief_id   ON helio_metrics(brief_id);
CREATE INDEX IF NOT EXISTS idx_food_brief_id    ON food_log(brief_id);
CREATE INDEX IF NOT EXISTS idx_weather_brief_id ON weather_log(brief_id);
CREATE INDEX IF NOT EXISTS idx_cal_brief_id     ON calendar_events(brief_id);
CREATE INDEX IF NOT EXISTS idx_tasks_brief_id   ON tasks(brief_id);

COMMIT;
