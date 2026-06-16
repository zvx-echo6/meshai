-- v0.5.8 persistence foundation: 13 data tables + event_log + schema_meta.
-- All timestamps are INTEGER epoch seconds (UTC) unless otherwise noted.
-- Booleans are INTEGER 0/1.

-- ============================================================================
-- schema_meta: key/value store; version key tracks applied migration.
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ============================================================================
-- CROSS-CUTTING: event_log
-- Every envelope received by the consumer lands here for auditing. Adapter
-- handlers fill table_name / table_pk after inserting into their specific
-- table; handled=1 marks success.
-- ============================================================================
CREATE TABLE IF NOT EXISTS event_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at       INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    category          TEXT,
    severity_word     TEXT,
    event_id_external TEXT,
    nats_subject      TEXT,
    handled           INTEGER NOT NULL DEFAULT 0,
    table_name        TEXT,
    table_pk          TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_log_received  ON event_log(received_at);
CREATE INDEX IF NOT EXISTS idx_event_log_source    ON event_log(source, received_at);
CREATE INDEX IF NOT EXISTS idx_event_log_handled   ON event_log(handled, received_at);

-- ============================================================================
-- traffic_events (state_511_atis + wzdx_federal)
-- Composite PK (source, external_id) so the same envelope re-arriving on
-- restart doesn't duplicate. last_seen_at gets bumped on every reissue;
-- last_broadcast_at records the most recent mesh broadcast for cooldown.
-- ============================================================================
CREATE TABLE IF NOT EXISTS traffic_events (
    source            TEXT    NOT NULL,   -- 'state_511_atis' | 'wzdx'
    external_id       TEXT    NOT NULL,
    road              TEXT,
    direction         TEXT,
    mile_start        INTEGER,
    mile_end          INTEGER,
    county            TEXT,
    state             TEXT,
    lat               REAL,
    lon               REAL,
    sub_type          TEXT,
    impact            TEXT,
    start_at          INTEGER,
    end_at            INTEGER,
    first_seen_at     INTEGER NOT NULL,
    last_seen_at      INTEGER NOT NULL,
    last_broadcast_at INTEGER,
    PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_traffic_last_seen      ON traffic_events(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_traffic_last_broadcast ON traffic_events(last_broadcast_at);
CREATE INDEX IF NOT EXISTS idx_traffic_state_county   ON traffic_events(state, county);

-- ============================================================================
-- fires (wfigs_incidents)
-- PK is IRWIN UUID. last_broadcast_acres + last_broadcast_contained enable
-- the 8h-rate-limit change-detection so subsequent updates that don't move
-- acres or containment can be suppressed.
-- ============================================================================
CREATE TABLE IF NOT EXISTS fires (
    irwin_id                  TEXT    PRIMARY KEY,
    incident_name             TEXT,
    incident_type             TEXT,
    current_acres             REAL,
    current_contained_pct     INTEGER,
    status                    TEXT,
    lat                       REAL,
    lon                       REAL,
    county                    TEXT,
    state                     TEXT,
    landclass                 TEXT,
    declared_at               INTEGER,
    last_event_at             INTEGER NOT NULL,
    last_broadcast_at         INTEGER,
    last_broadcast_acres      REAL,
    last_broadcast_contained  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fires_last_event     ON fires(last_event_at);
CREATE INDEX IF NOT EXISTS idx_fires_last_broadcast ON fires(last_broadcast_at);
CREATE INDEX IF NOT EXISTS idx_fires_state          ON fires(state);

-- ============================================================================
-- firms_pixels (v0.6 use; schema-ready now per Matt's request)
-- Nullable FK to fires(irwin_id) so unattached hotspots store without join.
-- v0.6 fire-tracker will populate irwin_id via spatial join.
-- ============================================================================
CREATE TABLE IF NOT EXISTS firms_pixels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    irwin_id    TEXT REFERENCES fires(irwin_id) ON DELETE SET NULL,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    acq_time    INTEGER NOT NULL,
    frp         REAL,
    confidence  TEXT,
    satellite   TEXT,
    brightness  REAL
);
CREATE INDEX IF NOT EXISTS idx_firms_pixels_acq_time ON firms_pixels(acq_time);
CREATE INDEX IF NOT EXISTS idx_firms_pixels_irwin    ON firms_pixels(irwin_id);
CREATE INDEX IF NOT EXISTS idx_firms_pixels_latlon   ON firms_pixels(lat, lon);

-- ============================================================================
-- quake_events (usgs_quake)
-- PK is USGS event id (e.g. ci10240102).
-- ============================================================================
CREATE TABLE IF NOT EXISTS quake_events (
    event_id          TEXT    PRIMARY KEY,
    magnitude         REAL,
    depth_km          REAL,
    place             TEXT,
    lat               REAL,
    lon               REAL,
    occurred_at       INTEGER,
    tsunami_warning   INTEGER NOT NULL DEFAULT 0,
    first_seen_at     INTEGER NOT NULL,
    last_broadcast_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quake_occurred       ON quake_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_quake_last_broadcast ON quake_events(last_broadcast_at);

-- ============================================================================
-- nws_alerts (nws)
-- PK is NWS CAP id (urn-style).
-- ============================================================================
CREATE TABLE IF NOT EXISTS nws_alerts (
    event_id          TEXT    PRIMARY KEY,
    alert_type        TEXT,
    severity          TEXT,
    county            TEXT,
    state             TEXT,
    headline          TEXT,
    description       TEXT,
    expires_at        INTEGER,
    first_seen_at     INTEGER NOT NULL,
    last_broadcast_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nws_expires        ON nws_alerts(expires_at);
CREATE INDEX IF NOT EXISTS idx_nws_state_severity ON nws_alerts(state, severity);

-- ============================================================================
-- gauge_readings (usgs nwis) -- time-series, autoincrement PK.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gauge_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         TEXT    NOT NULL,
    gauge_name      TEXT,
    reading_value   REAL,
    reading_unit    TEXT,
    threshold_state TEXT,                    -- normal | action | flood_minor | flood_moderate | flood_major
    flow_cfs        REAL,
    reading_time    INTEGER NOT NULL,
    lat             REAL,
    lon             REAL
);
CREATE INDEX IF NOT EXISTS idx_gauge_site_time ON gauge_readings(site_id, reading_time);
CREATE INDEX IF NOT EXISTS idx_gauge_time      ON gauge_readings(reading_time);
CREATE INDEX IF NOT EXISTS idx_gauge_threshold ON gauge_readings(threshold_state, reading_time);

-- ============================================================================
-- swpc_events (swpc) -- covers kindex / proton / alert.
-- payload_json holds the full structured payload for the event_type so we
-- don't have to migrate the schema every time a new SWPC product surfaces.
-- ============================================================================
CREATE TABLE IF NOT EXISTS swpc_events (
    event_id          TEXT    PRIMARY KEY,
    event_type        TEXT    NOT NULL,    -- kindex | proton | alert
    severity_int      INTEGER,
    payload_json      TEXT,
    occurred_at       INTEGER NOT NULL,
    first_seen_at     INTEGER NOT NULL,
    last_broadcast_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_swpc_occurred  ON swpc_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_swpc_type_time ON swpc_events(event_type, occurred_at);

-- ============================================================================
-- MESH TABLES
-- ============================================================================

-- mesh_nodes: per-node latest-known state + first/last seen + stale flag.
CREATE TABLE IF NOT EXISTS mesh_nodes (
    node_id          TEXT    PRIMARY KEY,   -- '!aabbccdd' or num as str
    long_name        TEXT,
    short_name       TEXT,
    hw_model         TEXT,
    last_lat         REAL,
    last_lon         REAL,
    last_altitude    REAL,
    last_battery_pct INTEGER,
    last_voltage     REAL,
    last_rssi        INTEGER,
    last_snr         REAL,
    neighbor_count   INTEGER,
    first_seen_at    INTEGER NOT NULL,
    last_seen_at     INTEGER NOT NULL,
    is_stale         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mesh_nodes_last_seen ON mesh_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_mesh_nodes_is_stale  ON mesh_nodes(is_stale);

-- mesh_telemetry: time-series; bucket='raw' incoming, rolled up to 'hourly'
-- / 'daily' by a retention job (not part of foundation).
CREATE TABLE IF NOT EXISTS mesh_telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id         TEXT    NOT NULL,
    recorded_at     INTEGER NOT NULL,
    battery_pct     INTEGER,
    voltage         REAL,
    current_ma      REAL,
    channel_util    REAL,
    air_util_tx     REAL,
    uptime_seconds  INTEGER,
    bucket          TEXT    NOT NULL DEFAULT 'raw'
);
CREATE INDEX IF NOT EXISTS idx_mesh_tel_node_time ON mesh_telemetry(node_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_mesh_tel_time      ON mesh_telemetry(recorded_at);
CREATE INDEX IF NOT EXISTS idx_mesh_tel_bucket    ON mesh_telemetry(bucket, recorded_at);

-- mesh_positions: time-series; bucket logic same as telemetry.
CREATE TABLE IF NOT EXISTS mesh_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       TEXT    NOT NULL,
    recorded_at   INTEGER NOT NULL,
    lat           REAL,
    lon           REAL,
    altitude      REAL,
    ground_speed  REAL,
    bucket        TEXT    NOT NULL DEFAULT 'raw'
);
CREATE INDEX IF NOT EXISTS idx_mesh_pos_node_time ON mesh_positions(node_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_mesh_pos_time      ON mesh_positions(recorded_at);
CREATE INDEX IF NOT EXISTS idx_mesh_pos_bucket    ON mesh_positions(bucket, recorded_at);

-- mesh_messages_in: inbound text traffic (broadcasts + DMs).
CREATE TABLE IF NOT EXISTS mesh_messages_in (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node    TEXT    NOT NULL,
    to_node      TEXT,                       -- NULL = channel broadcast
    channel      INTEGER,
    text         TEXT,
    received_at  INTEGER NOT NULL,
    portnum      TEXT,
    payload_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mesh_msg_in_received  ON mesh_messages_in(received_at);
CREATE INDEX IF NOT EXISTS idx_mesh_msg_in_from_node ON mesh_messages_in(from_node, received_at);

-- mesh_broadcasts_out: outbound meshai-originated mesh deliveries.
-- source_event_table/source_event_pk back-links to the table+pk that
-- triggered the broadcast (e.g. 'fires' / IRWIN id).
CREATE TABLE IF NOT EXISTS mesh_broadcasts_out (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at            INTEGER NOT NULL,
    recipient          TEXT    NOT NULL,     -- 'broadcast' or node_id
    channel            INTEGER,
    text               TEXT,
    source_event_table TEXT,
    source_event_pk    TEXT,
    bytes_sent         INTEGER,
    ack_received       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mesh_bcast_sent       ON mesh_broadcasts_out(sent_at);
CREATE INDEX IF NOT EXISTS idx_mesh_bcast_source     ON mesh_broadcasts_out(source_event_table, source_event_pk);

-- mesh_health_events: derived signal events (node went stale, recovered,
-- routing change detected, mesh score dropped, etc.). detail_json is the
-- structured payload; event_type is the canonical short tag.
CREATE TABLE IF NOT EXISTS mesh_health_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT    NOT NULL,
    node_id      TEXT,
    detected_at  INTEGER NOT NULL,
    severity     TEXT,
    detail_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mesh_hevt_detected ON mesh_health_events(detected_at);
CREATE INDEX IF NOT EXISTS idx_mesh_hevt_type     ON mesh_health_events(event_type, detected_at);
CREATE INDEX IF NOT EXISTS idx_mesh_hevt_node     ON mesh_health_events(node_id, detected_at);
