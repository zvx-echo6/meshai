-- v0.6-4 town_anchors curation table.
--
-- Replaces the hardcoded _TOWN_COORDS dict in central_normalizer.py.
-- The Python dict is removed in this commit; the lookup helpers in
-- central_normalizer.py now read from this table via
-- meshai.persistence.curation.lookup_town_anchor().
--
-- name TEXT is the canonical lookup key (lowercased) -- the existing
-- _compute_distance_bearing() already lowercases its town input. The
-- adapter accepting both 'Boise' and 'boise' is preserved because the
-- accessor lowercases before query.

CREATE TABLE IF NOT EXISTS town_anchors (
    anchor_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    state        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_town_anchors_enabled
    ON town_anchors(enabled);
