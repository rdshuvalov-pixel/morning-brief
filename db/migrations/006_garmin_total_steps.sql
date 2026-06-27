-- Morning Brief v2 — Garmin metrics: add total_steps + distance_km.
--
-- Garmin Connect API returns `totalSteps` and `distance` (in meters).
-- Currently garmin_metrics has no step count column — providers/garmin.py
-- silently drops it. After this migration, garmin_metrics.total_steps and
-- garmin_metrics.distance_km will receive daily values.
--
-- Renderer (playful/render_playful.py) reads steps with priority:
--   1. garmin_metrics.total_steps
--   2. helio_metrics.steps (fallback)
--   3. None (empty)
--
-- IMPORTANT: Table lives in `morning_brief_v2` schema (not `public`).
-- Supabase SQL Editor default search_path is "$user", public, so we set it
-- explicitly + use fully-qualified table names for safety.
--
-- Apply via Supabase SQL Editor on project dkmoocytmhzxhjzmodmj.
BEGIN;

SET LOCAL search_path = morning_brief_v2, public;

ALTER TABLE morning_brief_v2.garmin_metrics ADD COLUMN IF NOT EXISTS total_steps  INT;
ALTER TABLE morning_brief_v2.garmin_metrics ADD COLUMN IF NOT EXISTS distance_km  NUMERIC(6,2);

COMMIT;