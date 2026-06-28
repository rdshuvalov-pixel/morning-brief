-- Add body_battery_max to garmin_metrics
-- Peak body battery charge value for the day (Garmin Connect API field `max`)
--
-- IMPORTANT: Table lives in `morning_brief_v2` schema (not `public`).
-- Run in Supabase SQL Editor with search_path set:
--   SET search_path TO morning_brief_v2;
-- (See migrations/006_garmin_total_steps.sql for the gotcha.)

BEGIN;

ALTER TABLE garmin_metrics
    ADD COLUMN IF NOT EXISTS body_battery_max INT;

COMMIT;
