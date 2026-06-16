-- v0.6-3b firms_pixels dedup-key column + index update.
--
-- v6.sql created firms_pixels with a UNIQUE INDEX on
-- (round(lat, 5), round(lon, 5), acq_time, satellite) -- a hardcoded
-- ~1.1m precision. v0.6-3a.1 introduced adapter_config.firms.dedup_distance_m
-- (default 5m) for user-tunable dedup precision. SQLite indexes can't have
-- dynamic parameters, so we move to a precomputed `dedup_key` column the
-- handler quantizes at INSERT time.
--
-- The handler computes dedup_key = "<q_lat>,<q_lon>" where q_lat / q_lon
-- are lat/lon rounded to (dedup_distance_m / 111000) degrees. Two pixels
-- whose rounded coords agree get the same key and collide on the unique
-- index. Changing dedup_distance_m at runtime takes effect for future
-- INSERTs without touching the schema.
--
-- firms_pixels is empty at this migration -- production has 0 rows since
-- v0.6-1, so no backfill needed.

-- Drop the old hardcoded index.
DROP INDEX IF EXISTS idx_firms_pixels_dedup;

-- Add the explicit dedup_key column.
ALTER TABLE firms_pixels ADD COLUMN dedup_key TEXT;

-- New unique index on (dedup_key, acq_time, satellite). NULL dedup_key
-- compares unequal to other NULLs (SQLite default), so any legacy NULL
-- rows that exist on this DB don't trigger constraint violations against
-- each other or against future non-NULL rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_firms_pixels_dedup
    ON firms_pixels(dedup_key, acq_time, satellite);

-- Helper index for backfill queries / range scans by dedup_key alone.
CREATE INDEX IF NOT EXISTS idx_firms_pixels_dedup_key
    ON firms_pixels(dedup_key);
