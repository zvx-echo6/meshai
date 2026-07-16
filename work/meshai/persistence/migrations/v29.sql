-- v29 IPAWS civil-alert dedup table (ipaws adapter).
--
-- The IPAWS-OPEN EAS adapter (env/ipaws.py) broadcasts NON-weather civil
-- alerts (evacuation, Civil Emergency Message, AMBER, 911 outage, law
-- enforcement, HazMat). Its gating decider (notifications/gating/ipaws.py)
-- mirrors the NWS decider's first-sighting / Update / dedup-window /
-- tombstone logic but MUST NOT share the NWS nws_alerts table -- an IPAWS CAP
-- id and an NWS CAP id could otherwise collide, and their lifecycles are
-- independent. This is the IPAWS-owned equivalent of nws_alerts (v1.sql),
-- same column shape so the decider code is a near-verbatim parametrisation.
--
-- PK is the CAP <identifier> (urn-style), falling back to a synthetic
-- ipaws:<same>:<sent> key when a document omits one.
-- ============================================================================
CREATE TABLE IF NOT EXISTS ipaws_alerts (
    event_id          TEXT    PRIMARY KEY,
    alert_type        TEXT,
    severity          TEXT,
    county            TEXT,
    state             TEXT,
    headline          TEXT,
    description       TEXT,
    expires_at        INTEGER,
    first_seen_at     INTEGER NOT NULL,
    last_broadcast_at INTEGER,
    -- first_broadcast_at was added to nws_alerts et al. in v11; the ipaws
    -- decider's commit COALESCEs it, so this fresh table carries it inline.
    first_broadcast_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ipaws_expires        ON ipaws_alerts(expires_at);
CREATE INDEX IF NOT EXISTS idx_ipaws_state_severity ON ipaws_alerts(state, severity);
