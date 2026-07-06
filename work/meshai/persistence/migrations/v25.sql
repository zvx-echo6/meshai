-- v25 tropospheric ducting durable assessment table (LLM-queryable).
--
-- The native env/ducting.py adapter previously kept its RF-propagation
-- assessment in memory only (self._status / self._events), so the mesh LLM
-- (env_reporter) could not answer ducting / band-opening questions and the
-- assessment was lost on restart. Ducting is a single-point periodic
-- assessment, so we keep one durable current row per assessment location
-- (id = "ducting_{lat}_{lon}") that the writer UPSERTs each poll.
--
-- Persistence-only: does NOT affect broadcast/gating (tier hysteresis +
-- Event emission stay in the adapter/pipeline). IF NOT EXISTS so a fresh
-- install and a re-run are both no-ops.

CREATE TABLE IF NOT EXISTS ducting_events (
    id               TEXT    PRIMARY KEY,  -- ducting_{round(lat,2)}_{round(lon,2)}
    lat              REAL,
    lon              REAL,
    condition        TEXT,                 -- normal | super_refraction | surface_duct | elevated_duct
    tier             TEXT,                 -- normal | super_refraction | duct | surface_duct
    min_gradient     REAL,                 -- min modified-refractivity gradient (M-units/km)
    duct_base_m      REAL,
    duct_thickness_m REAL,
    assessment       TEXT,                 -- human-readable summary line
    assessed_at      INTEGER NOT NULL      -- epoch seconds of the assessment
);
CREATE INDEX IF NOT EXISTS idx_ducting_assessed ON ducting_events(assessed_at);
CREATE INDEX IF NOT EXISTS idx_ducting_tier     ON ducting_events(tier);
