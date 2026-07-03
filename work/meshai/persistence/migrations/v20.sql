-- v20: per-mesh broadcast audit. transport + success columns on
-- mesh_broadcasts_out so each SEND records which mesh it went to and
-- whether it landed. A broadcast fanning to BOTH meshes writes one row
-- per mesh, each with its own success flag.
-- Nullable, no index, no backfill. Legacy rows keep NULL transport/success
-- (Activity Log treats NULL as legacy/meshtastic-unknown).

ALTER TABLE mesh_broadcasts_out ADD COLUMN transport TEXT;
ALTER TABLE mesh_broadcasts_out ADD COLUMN success   INTEGER;
