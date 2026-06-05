-- v0.6-2 dispatcher state persistence (audit doc finding #1).
--
-- Pre-this-migration the dispatcher held its cold-start anchor, four drop
-- counters, per-(toggle,category,region) cooldown map, and (source,event_id)
-- dedup OrderedDict in instance memory only -- every container restart
-- silently reset them. The LLM (commit #5: env_reporter) needs cumulative
-- "we've dropped 47 stale events this week" semantics, which requires
-- write-through to SQLite.
--
-- All three tables are independent; the dispatcher's in-memory caches
-- remain authoritative on the fast path. Writes go to both. On boot the
-- in-memory caches are rebuilt from SQLite.

-- --------------------------------------------------------------------------
-- dispatcher_state: singleton row keyed on id=1. The CHECK constraint plus
-- the seed-INSERT-OR-IGNORE below guarantees exactly one row ever exists.
-- All counters are cumulative-since-install (NOT since-boot); the dispatcher
-- read-restores them on every __init__ and write-increments per drop.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispatcher_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    cold_start_anchor   REAL,                              -- epoch seconds; NULL until first event-to-enabled-toggle
    stale_dropped       INTEGER NOT NULL DEFAULT 0,
    cooldown_dropped    INTEGER NOT NULL DEFAULT 0,
    dedup_dropped       INTEGER NOT NULL DEFAULT 0,
    cold_start_dropped  INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL
);

-- Seed the singleton row. INSERT OR IGNORE so a re-run is a clean no-op.
INSERT OR IGNORE INTO dispatcher_state(id, cold_start_anchor,
    stale_dropped, cooldown_dropped, dedup_dropped, cold_start_dropped,
    updated_at)
VALUES (1, NULL, 0, 0, 0, 0, NULL);

-- --------------------------------------------------------------------------
-- dispatcher_cooldowns: one row per (toggle, category, region) triple that
-- has fired at least once. `last_fired_at` powers the "in cooldown?" check.
-- Pruned on every insert: rows older than (2 * current_cooldown_s) are
-- deleted (matches the existing in-memory prune semantics at
-- pipeline/dispatcher.py:183-187).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispatcher_cooldowns (
    toggle         TEXT NOT NULL,
    category       TEXT NOT NULL,
    region         TEXT NOT NULL,
    last_fired_at  REAL NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (toggle, category, region)
);
CREATE INDEX IF NOT EXISTS idx_dispatcher_cooldowns_last_fired
    ON dispatcher_cooldowns(last_fired_at);

-- --------------------------------------------------------------------------
-- dispatcher_dedup: bounded LRU on disk -- 7-day window (deleted on every
-- insert). The in-memory OrderedDict still caps at 10k for the fast path;
-- on boot we rebuild it from the most-recent 10k rows ordered by seen_at.
-- This means the DB retains MORE history than the in-memory cap, which is
-- useful for the LLM ("did we already see this event ID?") even if it has
-- been evicted from the in-memory LRU.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispatcher_dedup (
    source    TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    seen_at   REAL NOT NULL,
    PRIMARY KEY (source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_dispatcher_dedup_seen_at
    ON dispatcher_dedup(seen_at);
