-- v27 add severity_floor_dropped counter to dispatcher_state.
--
-- Prior to this migration the two severity-floor drop paths in
-- pipeline/dispatcher.py (toggle path + matrix per-cell path) silently
-- returned / continued with no log and no counter.  v27 wires a WARN log
-- and this cumulative counter so floor-drops are observable in ops/health
-- and in the LLM context (env_reporter).  The counter follows the same
-- write-through / restore pattern as the existing drop counters.
--
-- The migration runner (persistence/db.py) applies each version exactly
-- once (gated on schema_meta.version), so a plain ADD COLUMN is safe.

ALTER TABLE dispatcher_state ADD COLUMN severity_floor_dropped INTEGER NOT NULL DEFAULT 0;
