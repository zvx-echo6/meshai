-- v0.7-fire-tracker-1 -- registry correlation tables + per-fire spread radius.
--
-- Phase 1 of the FIRMS+WFIGS fusion design (v0.6-design-fire-tracker.md):
-- we make every incoming FIRMS pixel either attributable to a known fire
-- or a candidate for an early-warning "possible new fire" broadcast. This
-- migration adds the persistent state the FIRMS handler needs to do that;
-- the handler-side wiring lives in central/firms_handler.py.

-- ---- fire_pixels --------------------------------------------------------
-- Per-(fire, pixel) attribution history. Distinct from firms_pixels (which
-- is the raw inbound-event store -- one row per VIIRS detection regardless
-- of whether we can link it to a fire). fire_pixels exists so the centroid
-- query is a clean indexed (irwin_id, acq_time) range scan instead of a
-- join through firms_pixels. Phase 2's movement analysis will read this
-- table directly.

CREATE TABLE IF NOT EXISTS fire_pixels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    irwin_id      TEXT    NOT NULL REFERENCES fires(irwin_id) ON DELETE CASCADE,
    acq_time      REAL    NOT NULL,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    frp           REAL,
    satellite     TEXT,
    pass_id       TEXT,
    attributed_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fire_pixels_irwin_acq ON fire_pixels(irwin_id, acq_time);
CREATE INDEX IF NOT EXISTS idx_fire_pixels_acq       ON fire_pixels(acq_time);


-- ---- fires: per-fire override + running centroid + last hotspot --------
-- spread_radius_mi:
--     Per-fire attribution radius (statute miles). NULL means
--     "use the global default from adapter_config.fires.spread_radius_mi_default".
--     Per-fire overrides are intended for outliers (mega-fires with active
--     spotting, or contained fires where we want to tighten attribution to
--     avoid false-positive growth signals).
--
-- current_centroid_lat / current_centroid_lon:
--     The FIRMS-attributed running centroid (median of last 24h of
--     fire_pixels). Distinct from fires.lat/fires.lon (the WFIGS-declared
--     anchor) because a fire can drift miles from its initial declared
--     point as it grows; the centroid is where the fire actually IS
--     right now per satellite passes, while the anchor stays put for
--     reference. NULL until the first FIRMS attribution.
--
-- last_hotspot_at:
--     UNIX-epoch (REAL) timestamp of the most recent FIRMS pixel
--     attributed to this fire. Used in Phase 1 by the centroid query
--     (filter to last 24h) and by Phase 2's halt detector (no new
--     pixels for >= 2 satellite passes => fire considered halted).

ALTER TABLE fires ADD COLUMN spread_radius_mi REAL;
ALTER TABLE fires ADD COLUMN current_centroid_lat REAL;
ALTER TABLE fires ADD COLUMN current_centroid_lon REAL;
ALTER TABLE fires ADD COLUMN last_hotspot_at REAL;


-- ---- firms_pixels: cluster-tracking columns ----------------------------
-- attributed_at:
--     UNIX-epoch when this pixel was attributed to a fire via a
--     fire_pixels insertion. NULL means the pixel did NOT match any
--     known fire's spread radius -- it's a candidate for cluster
--     detection (unattributed_hotspot_cluster category).
--
-- cluster_broadcast_at:
--     UNIX-epoch when this pixel was covered by a fired
--     unattributed_hotspot_cluster broadcast. Stamped on every member
--     pixel of a broadcast so a subsequent pixel arriving in the same
--     cluster does not re-fire the broadcast for the same hotspots.

ALTER TABLE firms_pixels ADD COLUMN attributed_at        REAL;
ALTER TABLE firms_pixels ADD COLUMN cluster_broadcast_at REAL;

-- Compound index supporting the cluster query:
--   SELECT * FROM firms_pixels
--   WHERE attributed_at IS NULL AND cluster_broadcast_at IS NULL
--     AND acq_time > ?
-- The leading two columns are NULL-discriminator selective; acq_time
-- handles the time-window prune. (lat/lon prefilter is done by the
-- handler in Python after a bounding-box scan -- SQLite has no native
-- Haversine.)
CREATE INDEX IF NOT EXISTS idx_firms_pixels_unattributed
    ON firms_pixels(attributed_at, cluster_broadcast_at, acq_time);
