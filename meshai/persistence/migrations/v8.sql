-- v0.6-4 gauge_sites curation table.
--
-- Replaces the hardcoded IDAHO_CURATED_SITES dict in
-- meshai/central/idaho_gauge_sites.py with a GUI-editable SQLite table.
-- The Python dict is removed in this commit; the lookup helpers in
-- idaho_gauge_sites.py now read from this table via
-- meshai.persistence.curation.lookup_gauge_site().
--
-- Per-site NWS-AHPS thresholds (action / minor / moderate / major) are
-- nullable so sites without a published value for a band can sit on the
-- table without a fake value. Disabled rows (enabled=0) are skipped at
-- read time so the operator can pause a site without deleting it.

CREATE TABLE IF NOT EXISTS gauge_sites (
    site_id              TEXT PRIMARY KEY,            -- 'USGS-13139510' canonical
    gauge_name           TEXT NOT NULL,
    lat                  REAL NOT NULL,
    lon                  REAL NOT NULL,
    action_ft            REAL,
    flood_minor_ft       REAL,
    flood_moderate_ft    REAL,
    flood_major_ft       REAL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    updated_at           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gauge_sites_enabled
    ON gauge_sites(enabled);
