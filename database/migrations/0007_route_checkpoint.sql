-- 0007_route_checkpoint.sql
-- Route checkpoint for restart recovery (additive, repeat-safe). Together with 0006 (progress_m, last_eta_s,
-- progress_updated_at) a live route stores: progress, last ETA, the position of the last checkpoint, the route
-- segment it was on, when it was saved, and whether the route is currently unavailable (no drivable route).
ALTER TABLE routes
    ADD COLUMN IF NOT EXISTS checkpoint_lat      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS checkpoint_lon      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS checkpoint_segment  INTEGER,
    ADD COLUMN IF NOT EXISTS unavailable_reason  VARCHAR(500),
    ADD COLUMN IF NOT EXISTS unavailable_at      TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_routes_active_ambulance ON routes (ambulance_id) WHERE active;
