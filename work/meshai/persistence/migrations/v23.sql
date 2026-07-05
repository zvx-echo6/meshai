-- v23: observer_locations table for native satpass prediction.
--
-- The native SGP4 pass-predictor computes passes for a set of ground
-- observer stations. SatpassConfig.observers is the source of truth;
-- those entries are seeded (upserted) into this table on startup so the
-- predictor has a single queryable store of coordinates regardless of how
-- config was loaded. slug is the stable key.

CREATE TABLE IF NOT EXISTS observer_locations (
    slug     TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    alt_m    REAL NOT NULL DEFAULT 0,
    enabled  INTEGER NOT NULL DEFAULT 1
);
