-- v31 watchduty: Watch Duty (WD) enrichment columns for fires already
-- tracked via WFIGS/IRWIN, plus the reports-sent dedup table used by a
-- later group.
--
-- Watch Duty is an ENRICHMENT source only -- it never creates a fire row.
-- meshai matches an already-broadcast WFIGS fire to a Watch Duty
-- geo_event by proximity (env/watchduty.py::match_fires) and stamps the
-- match onto the existing fires row:
--
--   watchduty_event_id   -- WD geo_event id once matched, else NULL.
--   watchduty_name       -- WD's own display name for the incident
--                            (refreshed on every poll while matched).
--   watchduty_matched_at -- epoch when the match was first made.
--   watchduty_is_active  -- WD's own is_active flag, refreshed on every
--                            poll; used to decide whether the fire is
--                            still worth polling for (see
--                            WatchDutyAdapter._should_poll).
--
-- The remaining four columns are unused until a later group wires them
-- up, but ship now so the schema doesn't need a second migration:
--
--   watchduty_evac_state        -- Group B: evacuation level text/state.
--   watchduty_evac_zone_text    -- Group B: normalized zone description.
--   watchduty_evac_updated_at   -- Group B: epoch of WD's own last update.
--   watchduty_evac_broadcast_at -- Group B: epoch of our last evac
--                                   broadcast for this fire, used for the
--                                   same-level text-edit hourly cooldown.
--
-- watchduty_reports_sent (Group C) is the dedup ledger for individual WD
-- report broadcasts: one row per WD report_id we have sent (or seeded as
-- already-seen on first match, seeded=1) so a report is never broadcast
-- twice.

ALTER TABLE fires ADD COLUMN watchduty_event_id TEXT;
ALTER TABLE fires ADD COLUMN watchduty_name TEXT;
ALTER TABLE fires ADD COLUMN watchduty_matched_at REAL;
ALTER TABLE fires ADD COLUMN watchduty_is_active INTEGER;
ALTER TABLE fires ADD COLUMN watchduty_evac_state TEXT;          -- used by Group B
ALTER TABLE fires ADD COLUMN watchduty_evac_zone_text TEXT;      -- Group B
ALTER TABLE fires ADD COLUMN watchduty_evac_updated_at REAL;     -- Group B
ALTER TABLE fires ADD COLUMN watchduty_evac_broadcast_at REAL;   -- Group B (same-level text-edit hourly cooldown)

CREATE INDEX IF NOT EXISTS idx_fires_watchduty ON fires(watchduty_event_id) WHERE watchduty_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS watchduty_reports_sent (
    report_id     TEXT    PRIMARY KEY,
    irwin_id      TEXT    NOT NULL REFERENCES fires(irwin_id) ON DELETE CASCADE,
    geo_event_id  TEXT,
    sent_at       REAL,
    seeded        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL
);  -- Group C
CREATE INDEX IF NOT EXISTS idx_watchduty_reports_irwin ON watchduty_reports_sent(irwin_id);
