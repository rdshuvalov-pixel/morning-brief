-- Morning Brief v2 — Helio metrics extended columns.
-- Phase 2 of t_126ec3e4: add fields that the Zepp API exposes but helio_metrics
-- schema didn't yet capture. skin_temp comes from readiness/watch_score items[0]
-- .value.skinTempCalibrated (already parsed by backfill_helio._parse_readiness).
-- pai (Personal Activity Intelligence) comes from PaiHealthInfo user-event.
-- respiratory_rate comes from RespiratoryRate/real_data watch-event.
--
-- Apply via Supabase SQL Editor on project dkmoocytmhzxhjzmodmj (morning_brief_v2 schema).
-- After applying, run:
--     cd /root/morning_brief_v2 && set -a && . ./.env && set +a
--     ./.venv/bin/python run_helio_yesterday.py
BEGIN;

ALTER TABLE helio_metrics ADD COLUMN IF NOT EXISTS skin_temp        NUMERIC(5,2);
ALTER TABLE helio_metrics ADD COLUMN IF NOT EXISTS pai              INT;
ALTER TABLE helio_metrics ADD COLUMN IF NOT EXISTS respiratory_rate NUMERIC(5,2);

COMMIT;