-- v0.6-phase3 schema split: first_broadcast_at vs last_broadcast_at +
-- adapter_meta.reminder_enabled.
--
-- Pre-this-migration every adapter table tracked only last_broadcast_at,
-- so the wire-string prefix logic couldn t tell "this is the first time
-- we ve ever announced this row" from "this is the Nth Active: reminder".
-- The split lets the handler / ReminderScheduler stamp:
--     New:    first_broadcast_at IS NULL  (first sight)
--     Update: WFIGS material change (handler-level)
--     Active: ReminderScheduler clock fire (first_broadcast_at IS NOT NULL)
--
-- Backfill: any existing row whose last_broadcast_at IS NOT NULL gets its
-- first_broadcast_at = last_broadcast_at -- we don t know the actual first
-- time but treating the existing broadcast time as both is the conservative
-- read ("first observable broadcast we have a record of").

ALTER TABLE fires           ADD COLUMN first_broadcast_at REAL;
ALTER TABLE nws_alerts      ADD COLUMN first_broadcast_at REAL;
ALTER TABLE traffic_events  ADD COLUMN first_broadcast_at REAL;
ALTER TABLE quake_events    ADD COLUMN first_broadcast_at REAL;
ALTER TABLE swpc_events     ADD COLUMN first_broadcast_at REAL;
ALTER TABLE gauge_readings  ADD COLUMN first_broadcast_at REAL;

UPDATE fires          SET first_broadcast_at = last_broadcast_at WHERE last_broadcast_at IS NOT NULL;
UPDATE nws_alerts     SET first_broadcast_at = last_broadcast_at WHERE last_broadcast_at IS NOT NULL;
UPDATE traffic_events SET first_broadcast_at = last_broadcast_at WHERE last_broadcast_at IS NOT NULL;
UPDATE quake_events   SET first_broadcast_at = last_broadcast_at WHERE last_broadcast_at IS NOT NULL;
UPDATE swpc_events    SET first_broadcast_at = last_broadcast_at WHERE last_broadcast_at IS NOT NULL;
-- gauge_readings has no last_broadcast_at column -- the time-series table
-- inserts a new row per reading. The first_broadcast_at column is added
-- for schema uniformity but the ReminderScheduler doesn t scan it today.

-- adapter_meta: which adapters PRODUCE clock-driven reminders.
ALTER TABLE adapter_meta ADD COLUMN reminder_enabled INTEGER NOT NULL DEFAULT 0;

-- Default reminder_enabled=1 for the three adapter families that opt in.
-- The itd_511_work_zone row itself is seeded by adapter_config.defaults in
-- the same boot via INSERT OR IGNORE; this UPDATE just ensures the flag
-- holds when the row already exists.
UPDATE adapter_meta SET reminder_enabled = 1 WHERE adapter IN ('wfigs', 'swpc');
