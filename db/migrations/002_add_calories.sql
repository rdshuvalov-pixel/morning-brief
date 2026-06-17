-- Add resting and active calories to garmin_metrics
ALTER TABLE garmin_metrics ADD COLUMN IF NOT EXISTS resting_kcal INT;
ALTER TABLE garmin_metrics ADD COLUMN IF NOT EXISTS active_kcal  INT;

-- Add distance to helio_metrics
ALTER TABLE helio_metrics ADD COLUMN IF NOT EXISTS distance INT;
