-- !addme (MeshCore self-service contact add) audit trail: which pubkeys
-- AIDA has added as a contact on a sender's behalf, how it resolved them,
-- and when. Not used for gating (the live MeshCore contact list is the
-- source of truth for "already a contact") -- purely for auditability.
CREATE TABLE IF NOT EXISTS addme_provenance (
    pubkey     TEXT PRIMARY KEY,
    name       TEXT,
    source     TEXT NOT NULL,
    added_at   REAL NOT NULL
);
