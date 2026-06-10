-- v0.7-fire-tracker-4 -- daily fire digest broadcast tracking.
--
-- Phase 4 feature. The digest scheduler fires N broadcasts/day for the
-- mesh ("Fires today: Cache Peak +200 ac NE; Twin Peaks stable"). Each
-- slot is uniquely identified by its UNIX-epoch wall-clock time; the
-- INSERT OR IGNORE pattern (used by band_conditions_broadcasts) keeps
-- a container restart from double-firing the same slot.

CREATE TABLE IF NOT EXISTS fire_digest_broadcasts (
    slot_epoch    INTEGER PRIMARY KEY,    -- target slot in UNIX epoch seconds
    sent_at       INTEGER NOT NULL,        -- when we actually broadcast
    summary       TEXT,                    -- the LLM-rendered wire (audit)
    source        TEXT NOT NULL            -- 'llm' | 'fallback_terse' | 'skipped_no_fires'
);

CREATE INDEX IF NOT EXISTS idx_fire_digest_sent_at
    ON fire_digest_broadcasts(sent_at);
