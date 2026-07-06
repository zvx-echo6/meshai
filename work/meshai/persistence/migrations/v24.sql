-- v24 avalanche durable hazard table (LLM-queryable).
--
-- The native env/avalanche.py adapter previously kept advisories in memory
-- only (self._events), so the mesh LLM (env_reporter) was blind to avalanche
-- danger and state was lost on restart. This table gives each active zone a
-- durable row keyed by the adapter's stable event_id ("avy_{center}_{zone}").
--
-- Persistence-only: does NOT affect broadcast/gating (that stays driven by
-- the pipeline + adapter_config). NAADS danger scale 1-5. IF NOT EXISTS so a
-- fresh install and a re-run are both no-ops.

CREATE TABLE IF NOT EXISTS avalanche_events (
    event_id       TEXT    PRIMARY KEY,   -- avy_{center_id}_{zone}
    center_id      TEXT,
    zone_name      TEXT,
    danger_level   INTEGER,               -- NAADS 1-5 (-1/0 = no rating)
    danger_name    TEXT,
    travel_advice  TEXT,
    lat            REAL,
    lon            REAL,
    expires_at     INTEGER,               -- epoch seconds; end-of-day validity
    first_seen_at  INTEGER NOT NULL,
    last_event_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_avalanche_expires ON avalanche_events(expires_at);
CREATE INDEX IF NOT EXISTS idx_avalanche_center  ON avalanche_events(center_id);
CREATE INDEX IF NOT EXISTS idx_avalanche_danger  ON avalanche_events(danger_level);
