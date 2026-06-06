-- v0.7-fire-tracker-3 -- spotting detection (per-pass perimeter + spotting latch).
--
-- Phase 3 of the FIRMS+WFIGS fusion. Phase 2 made every closed pass
-- show up as a fire_passes row; Phase 3 adds the perimeter (convex hull
-- of pass pixels) stored as GeoJSON, plus a per-fire cooldown latch
-- that prevents rapid embers from spamming the mesh.

-- ---- fire_passes: perimeter_geojson -----------------------------------
-- Computed at pass close (when a new pass first attributes a pixel for
-- this fire). NULL until then. GeoJSON Polygon, single outer ring in
-- (lon, lat) order per RFC 7946; first coordinate repeated at the end
-- so the ring is closed. Convex hull (Andrew's monotone chain) -- not
-- a true alpha-shape perimeter, but the design doc accepts vertex-
-- distance approximation for spotting checks.

ALTER TABLE fire_passes ADD COLUMN perimeter_geojson TEXT;


-- ---- fires: per-fire spotting cooldown latch ---------------------------
-- last_spotting_broadcast_at:
--     Stamped each time a wildfire_spotting broadcast fires for this
--     fire. The next spotting candidate within the cooldown window
--     (adapter_config.fires.spotting_cooldown_seconds) is silently
--     suppressed -- the broadcast already went out, repeating it would
--     just spam. Cleared (implicitly) when NOW - this >= cooldown.

ALTER TABLE fires ADD COLUMN last_spotting_broadcast_at REAL;

-- Index for the cooldown probe (read every attributed pixel arrives).
CREATE INDEX IF NOT EXISTS idx_fires_last_spotting
    ON fires(irwin_id, last_spotting_broadcast_at);
