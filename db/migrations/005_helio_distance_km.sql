-- Morning Brief v2 — Helio metrics: add distance_km.
--
-- Derived from `steps × STRIDE_M / 1000` (STRIDE_M = 0.762 m, agreed 2026-06-22).
-- Providers/helio_parsers.parse_daily_health writes this field; the PGRST204
-- filter in providers/helio.py + backfill_helio.py currently drops it because
-- the column is missing.
--
-- Apply via Supabase SQL Editor on project dkmoocytmhzxhjzmodmj (morning_brief_v2 schema),
-- or via psql if a service-role connection is available.
BEGIN;

ALTER TABLE helio_metrics ADD COLUMN IF NOT EXISTS distance_km NUMERIC(6,2);

COMMIT;
