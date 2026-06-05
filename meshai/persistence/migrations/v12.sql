-- v0.6-tail item 4: WFIGS tombstone column on fires.
--
-- The ReminderScheduler's "tombstone" termination condition for wfigs
-- previously had nothing to check -- the fires table didn't carry a
-- tombstone flag, so reminders kept firing for fires WFIGS had since
-- marked closed. This column closes that gap.
--
-- wfigs_handler.py stamps `tombstoned_at = NOW` on the tombstone branch
-- (kind=='wfigs_tombstone'). ReminderScheduler treats `tombstoned_at IS
-- NOT NULL` as "stop reminding". Nullable + no default so the column
-- carries the precise tombstone time (used by env_reporter and the LLM
-- for "when did this fire close out?" questions).

ALTER TABLE fires ADD COLUMN tombstoned_at REAL;

CREATE INDEX IF NOT EXISTS idx_fires_tombstoned_at
    ON fires(tombstoned_at);
