-- v18: satpass_pending table for observer consolidation.
-- Accumulates per-observer pass data for 5s before consolidating
-- into a single broadcast per satellite per hour bucket.

CREATE TABLE IF NOT EXISTS satpass_pending (
    consolidated_id TEXT NOT NULL,
    observer        TEXT NOT NULL,
    sat_name        TEXT,
    norad_id        INTEGER,
    max_elevation   REAL,
    aos_at          INTEGER,
    los_at          INTEGER,
    aos_compass     TEXT,
    los_compass     TEXT,
    received_at     INTEGER,
    PRIMARY KEY (consolidated_id, observer)
);
