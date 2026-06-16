-- v0.6-1 firms_pixels dedup index.
--
-- The firms_pixels table was created in v1.sql (v0.5.8b) but had no writer
-- until v0.6-1 (firms_handler.py). FIRMS publishes the same satellite pixel
-- multiple times via NATS reconnect / JetStream replay; without a dedup
-- key, every restart would re-store the entire 7-day retention window.
--
-- Round lat/lon to 5 decimals (~1.1 m precision, well inside the 375 m
-- VIIRS pixel) so float-noise variants of the same coord collapse onto
-- the same key. `acq_time` is epoch seconds (UTC) parsed from FIRMS'
-- acq_date + acq_time fields. `satellite` is "N" (Suomi-NPP) or "N20"
-- (NOAA-20); future MODIS adds "Terra"/"Aqua".
--
-- The handler uses INSERT OR IGNORE so a duplicate is a clean no-op and
-- the caller can distinguish via cur.rowcount.

CREATE UNIQUE INDEX IF NOT EXISTS idx_firms_pixels_dedup
    ON firms_pixels(round(lat, 5), round(lon, 5), acq_time, satellite);
