-- v22: due_at column on satpass_pending.
-- Persists the epoch-second fire time (received_at + baseline consolidation
-- delay) for each pending observer row so a restart can reconstruct the
-- consolidation timers that previously lived only as in-memory asyncio
-- TimerHandles and were orphaned on reboot. Nullable; legacy pending rows
-- (there are none at rest — the table is drained per bucket) keep NULL and
-- the startup sweep falls back to received_at + baseline delay for them.

ALTER TABLE satpass_pending ADD COLUMN due_at INTEGER;
