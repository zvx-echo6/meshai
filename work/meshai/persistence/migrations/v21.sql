-- v21: peak_compass column on satpass_pending.
-- Carries the compass direction at a pass's PEAK elevation through the
-- observer-consolidation buffer so the consolidated broadcast wire can
-- render aos→peak→los. Nullable; legacy pending rows (there are none at
-- rest — the table is drained per bucket) keep NULL and render aos→los.

ALTER TABLE satpass_pending ADD COLUMN peak_compass TEXT;
