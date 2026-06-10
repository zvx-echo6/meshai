-- v0.6-6 pipeline persistence: inhibit_state + grouper_held.
--
-- Inhibitor + Grouper state survived only in instance memory pre-v0.6-6,
-- so every container restart dropped the inhibit-key TTL window AND the
-- pending coalescing groups. The LLM (via env_reporter.build_drop_audit)
-- couldn t answer "what's currently being suppressed?" because that
-- information never reached disk.
--
-- Both tables are write-through: handle() writes the same data to memory
-- and to disk. On boot, Inhibitor/Grouper restore from these tables
-- (filtered to non-expired rows) before processing any new events.

CREATE TABLE IF NOT EXISTS inhibit_state (
    key         TEXT    PRIMARY KEY,
    rank        INTEGER NOT NULL,
    expires_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inhibit_state_expires
    ON inhibit_state(expires_at);

CREATE TABLE IF NOT EXISTS grouper_held (
    group_key       TEXT PRIMARY KEY,
    event_json      TEXT NOT NULL,
    hold_until_at   REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grouper_held_hold_until
    ON grouper_held(hold_until_at);
