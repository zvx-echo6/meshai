-- v0.5.9 schema migration: add incident-handler columns to traffic_events.
--
-- The v0.5.8b traffic_events table covered state_511_atis + wzdx with road/
-- direction/mile_post/county/state fields. The unified incident handler
-- (tomtom_incidents + state_511_atis incidents/closures + itd_511) needs:
--
--   * `magnitude_of_delay`        : TomTom 0-4 severity int, NULL for the
--                                    511 sources that don't carry it
--   * `delay_seconds`             : TomTom delay measurement, NULL otherwise
--   * `icon_category`             : canonical sub_type word (cross-source)
--   * `last_broadcast_magnitude`  : snapshot of magnitude when we last fired
--   * `last_broadcast_delay_seconds` : snapshot of delay when we last fired
--   * `last_broadcast_icon_category` : snapshot of icon_category when we last fired
--
-- The three `last_broadcast_*` columns power per-incident change-detection
-- (Matt's v0.5.9 spec §5): broadcast Update only when magnitude bumps up,
-- delay doubles, icon_category changes, or 8h heartbeat fires. SQLite stores
-- NULL columns cheaply so the work-zone adapters can ignore these fields.

ALTER TABLE traffic_events ADD COLUMN magnitude_of_delay INTEGER;
ALTER TABLE traffic_events ADD COLUMN delay_seconds INTEGER;
ALTER TABLE traffic_events ADD COLUMN icon_category TEXT;
ALTER TABLE traffic_events ADD COLUMN last_broadcast_magnitude INTEGER;
ALTER TABLE traffic_events ADD COLUMN last_broadcast_delay_seconds INTEGER;
ALTER TABLE traffic_events ADD COLUMN last_broadcast_icon_category TEXT;

-- Index for the change-detection hot path (per-source per-external_id lookup
-- is already covered by the composite PK; this index helps the "who hasn't
-- broadcast in 8h" sweep queries that a watchdog will use in v0.5.10).
CREATE INDEX IF NOT EXISTS idx_traffic_source_lastbcast
    ON traffic_events(source, last_broadcast_at);
