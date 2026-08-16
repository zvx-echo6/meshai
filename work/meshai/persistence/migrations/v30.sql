-- v30 custom_announcements: user-crafted scheduled announcements.
--
-- Free-text broadcasts the owner types straight into the GUI ("at 08:00
-- every day, send: <literally anything>") -- NO placeholders, NO templating,
-- NO data source. Distinct from every other scheduled broadcast in this
-- codebase (band_conditions, reminders, wzdx_summary) in that it carries its
-- OWN explicit destination list (`channels`) instead of routing through a
-- toggle + region_routes matrix -- see dispatch_scheduled_custom_broadcast()
-- in notifications/pipeline/dispatcher.py.
--
-- schedule_kind determines which of the cadence columns is authoritative:
--   daily         -- time_of_day only.
--   interval_days -- time_of_day + interval_days (anchored at created_at's
--                    local calendar date -- see CustomAnnouncementScheduler).
--   weekly        -- time_of_day + dow_mask (JSON list of 7 booleans,
--                    Mon-first, i.e. index 0 = Monday .. 6 = Sunday).
--   monthly       -- time_of_day + day_of_month (1-31; clamped to the last
--                    day of shorter months at fire time, e.g. 31 fires on
--                    Feb 28/29).
--
-- channels is a JSON list of {"transport": "meshtastic"|"meshcore",
-- "channel": <int index>|<str name>, "name": <optional display name>} --
-- UNBOUNDED length, arbitrary mix of transports, no "primary channel"
-- concept. Every entry gets its own delivery + its own mesh_broadcasts_out
-- audit row (dispatch_scheduled_custom_broadcast loops the list).
--
-- New announcements start DISABLED (enabled=0) -- the owner arms them
-- explicitly after reviewing via POST /api/announcements/{id}/preview.
-- There is deliberately NO "send now" endpoint anywhere in this feature.
--
-- last_sent_at doubles as the scheduler's restart-safe dedup marker: it is
-- stamped with the actual send epoch, and the scheduler compares the LOCAL
-- CALENDAR DATE (in `timezone`) of last_sent_at against the local date of
-- the slot under consideration -- since every schedule_kind fires at most
-- once per local calendar day, same-date == already-sent-today, and this
-- survives a restart because it is written through to SQLite immediately
-- after a successful send (see CustomAnnouncementScheduler._maybe_fire).

CREATE TABLE IF NOT EXISTS custom_announcements (
    announcement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    message         TEXT    NOT NULL,
    schedule_kind   TEXT    NOT NULL,   -- daily | interval_days | weekly | monthly
    time_of_day     TEXT    NOT NULL,   -- "HH:MM"
    interval_days   INTEGER,            -- interval_days: N (2 = every other day)
    dow_mask        TEXT,               -- weekly: JSON list of 7 booleans, Mon-first
    day_of_month    INTEGER,            -- monthly: 1-31, clamped at fire time
    timezone        TEXT    NOT NULL DEFAULT 'America/Boise',
    channels        TEXT    NOT NULL,   -- JSON list of {"transport","channel","name"?}
    enabled         INTEGER NOT NULL DEFAULT 0,
    last_sent_at    REAL,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_custom_announcements_enabled ON custom_announcements(enabled);
CREATE INDEX IF NOT EXISTS idx_custom_announcements_kind    ON custom_announcements(schedule_kind);
