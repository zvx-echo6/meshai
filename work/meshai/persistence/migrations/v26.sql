-- v26 generic config-driven source events (LLM-queryable durable store).
--
-- The universal GenericHttpAdapter (env/generic_http.py) polls any public
-- REST/GeoJSON feed described in config.generic_sources[] and maps its items
-- onto meshai Events. This table is that adapter's durable store so the mesh
-- LLM (env_reporter.build_generic_detail) can answer questions about any
-- configured source, and so cold-start-silent seeding survives restarts.
--
-- One row per (source, event_id): source is the bare configured source name
-- (e.g. "idaho_power"); event_id is the item's stable id (id_path). The store
-- upserts every polled item (seeded or newly broadcast) here. data_json holds
-- the full mapped data dict (title + field_mappings + geometry, etc.).
--
-- Persistence-only: does NOT affect broadcast/gating (the received-delta gate
-- + coverage/decider path stay in the store/pipeline). IF NOT EXISTS so a
-- fresh install and a re-run are both no-ops.

CREATE TABLE IF NOT EXISTS generic_events (
    source      TEXT    NOT NULL,   -- bare configured source name
    event_id    TEXT    NOT NULL,   -- stable per-item id (id_path)
    category    TEXT,               -- configured category (e.g. power_outage)
    title       TEXT,               -- resolved title_path value
    lat         REAL,
    lon         REAL,
    severity    TEXT,               -- routine | priority | immediate
    data_json   TEXT,               -- full mapped data dict as JSON
    first_seen  INTEGER,            -- epoch seconds first ingested
    last_seen   INTEGER,            -- epoch seconds most recently seen
    PRIMARY KEY (source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_generic_last_seen ON generic_events(last_seen);
CREATE INDEX IF NOT EXISTS idx_generic_source    ON generic_events(source);
