-- v0.7-fire-tracker-2 -- per-satellite-pass tracking + drift state.
--
-- Phase 2 of the FIRMS+WFIGS fusion. Phase 1 made every attributed
-- FIRMS pixel land in fire_pixels with a pass_id (satellite + 90 min
-- bucket); Phase 2 closes the loop by computing per-pass aggregate
-- state and using consecutive-pass drift to detect growth + halt.

-- ---- fire_passes -------------------------------------------------------
-- One row per (irwin_id, pass_id). Updated on every attributed pixel
-- for that pass via UPSERT -- the pass row's stats reflect every pixel
-- attributed to the fire in that satellite/time bucket. drift_* are
-- populated on the boundary-detect path (first pixel of a new pass)
-- by walking back to the prior pass's centroid in this table.

CREATE TABLE IF NOT EXISTS fire_passes (
    irwin_id           TEXT    NOT NULL REFERENCES fires(irwin_id) ON DELETE CASCADE,
    pass_id            TEXT    NOT NULL,
    pass_centroid_lat  REAL    NOT NULL,
    pass_centroid_lon  REAL    NOT NULL,
    pixel_count        INTEGER NOT NULL,
    total_frp          REAL,
    pass_started_at    REAL    NOT NULL,  -- min(acq_time) in pass
    pass_ended_at      REAL    NOT NULL,  -- max(acq_time) in pass
    drift_mi_from_prev REAL,               -- Haversine to prior pass centroid
    drift_direction    TEXT,               -- 8-way N/NE/E/SE/S/SW/W/NW
    drift_mi_per_hour  REAL,               -- drift / wall-clock delta hours
    PRIMARY KEY (irwin_id, pass_id)
);

-- The growth / halt queries both range-scan by (irwin_id, pass_ended_at):
--   - growth: find the prior pass's centroid (DESC LIMIT 1)
--   - halt:   count passes that landed AFTER fires.last_pass_at
CREATE INDEX IF NOT EXISTS idx_fire_passes_irwin_ended
    ON fire_passes(irwin_id, pass_ended_at);


-- ---- fires: last-pass cursor + halt-broadcast latch --------------------
-- last_pass_id / last_pass_at:
--     Cursor for the most recent satellite pass that contributed pixels
--     to this fire. The FIRMS handler compares incoming pixel's pass_id
--     against last_pass_id to detect a boundary; on boundary it walks
--     back to the prior pass for the drift computation and fires
--     wildfire_growth when drift >= growth_drift_threshold_mi.
--
-- halt_broadcast_at:
--     Set when the halt detector emits wildfire_halted for this fire.
--     Latched so a subsequent halt-detector pass doesn't re-fire on the
--     same idle fire. Cleared (implicitly) when last_pass_at is updated
--     by a new pass arrival -- a fire that comes back to life will get
--     halt_broadcast_at left as a historical timestamp but will be
--     re-eligible for halt if it goes idle again, because the halt
--     detector also gates on (last_pass_at > halt_broadcast_at).

ALTER TABLE fires ADD COLUMN last_pass_id      TEXT;
ALTER TABLE fires ADD COLUMN last_pass_at      REAL;
ALTER TABLE fires ADD COLUMN halt_broadcast_at REAL;
