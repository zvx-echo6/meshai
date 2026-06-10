-- v0.5.11 schema migration: band_conditions_broadcasts.
--
-- One row per band-conditions broadcast attempt (3x/day). Inserted
-- whether or not the broadcast actually went out -- source='skipped_no_data'
-- accounts for the cases where neither local SWPC data nor HamQSL.com
-- fallback yielded usable ratings, so the scheduler skipped silently.
--
-- UNIQUE(scheduled_for) enforces per-slot dedup: if the scheduler fires
-- multiple times for the same target hour (e.g. retry loop, clock drift),
-- only the first INSERT wins. Caller checks the constraint via
-- INSERT OR IGNORE so a duplicate firing is a clean no-op.

CREATE TABLE IF NOT EXISTS band_conditions_broadcasts (
    broadcast_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at        INTEGER,
    scheduled_for  INTEGER NOT NULL,
    ratings_json   TEXT,
    source         TEXT NOT NULL,
    UNIQUE(scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_band_conditions_sent
    ON band_conditions_broadcasts(sent_at);
CREATE INDEX IF NOT EXISTS idx_band_conditions_scheduled
    ON band_conditions_broadcasts(scheduled_for);
