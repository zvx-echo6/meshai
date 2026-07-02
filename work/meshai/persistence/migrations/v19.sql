-- v19: fire_cause, unique_fire_id, geocoder_city columns for fire-alert enrichment.
-- Nullable TEXT, no index, no backfill.

ALTER TABLE fires ADD COLUMN fire_cause     TEXT;
ALTER TABLE fires ADD COLUMN unique_fire_id TEXT;
ALTER TABLE fires ADD COLUMN geocoder_city  TEXT;
