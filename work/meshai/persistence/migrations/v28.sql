-- v28 durable retention for the passive mesh-context buffer (MeshContext).
--
-- MeshContext._buffer is an in-memory deque(maxlen=50000) that observes all
-- mesh traffic (channel broadcasts, DMs, BBS notifications) for LLM recap
-- context -- but it was wiped on every container restart, so recap requests
-- right after a restart got an empty buffer even though max_age keeps
-- observations "live" for up to config.context.max_age seconds (14d
-- default). This table is the write-through / load-on-boot backing store so
-- observations survive restarts, following the same durable-store pattern
-- as generic_events (v26) and satpass_events.
--
-- One row per observed message. transport distinguishes meshtastic vs
-- meshcore (MeshContext.get_context_block filters by it). is_dm stored as
-- 0/1 (SQLite has no native bool). Indexed on ts for the age-cutoff prune
-- query and the load-on-boot "most recent N within max_age" query.

CREATE TABLE IF NOT EXISTS mesh_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,   -- epoch seconds (MeshObservation.timestamp)
    transport   TEXT    NOT NULL,   -- "meshtastic" | "meshcore"
    channel     INTEGER,            -- channel index
    is_dm       INTEGER NOT NULL DEFAULT 0,
    sender_name TEXT,
    sender_id   TEXT,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_mesh_observations_ts ON mesh_observations(ts);
