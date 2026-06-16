-- v0.7 Tier 2 satellite pass support.
--
-- sat_tles: cached TLE elements from Central's CENTRAL_SAT stream
-- (central.sat.tle.>). ~190 satellites refreshed every ~4h.
-- Staleness excluded at READ time (epoch > 14 days = stale).
--
-- satpass_events: Tier 1 table (IF NOT EXISTS for fresh installs
-- that skipped the handler-level CREATE).

CREATE TABLE IF NOT EXISTS sat_tles (
    norad_id    INTEGER PRIMARY KEY,
    name        TEXT,
    line1       TEXT NOT NULL,
    line2       TEXT NOT NULL,
    epoch       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS satpass_events (
    event_id          TEXT PRIMARY KEY,
    norad_id          INTEGER,
    sat_name          TEXT,
    observer          TEXT,
    max_elevation     REAL,
    aos_at            INTEGER,
    los_at            INTEGER,
    payload_json      TEXT,
    first_seen_at     INTEGER,
    first_broadcast_at INTEGER,
    last_broadcast_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_satpass_norad ON satpass_events(norad_id);
CREATE INDEX IF NOT EXISTS idx_satpass_observer ON satpass_events(observer);
CREATE INDEX IF NOT EXISTS idx_satpass_aos ON satpass_events(aos_at);
