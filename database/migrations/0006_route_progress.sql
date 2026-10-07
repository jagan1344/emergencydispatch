-- 0006_route_progress.sql
-- Telemetry persistence for restart recovery: the live route's progress and last ETA are stored with the route
-- (written by the telemetry flush), and every stored GPS fix keeps its progress along the route.
-- Existing columns already hold the rest: ambulances.latitude/longitude/last_updated/status/current_incident/
-- destination*, emergency_incidents.destination_hospital, routes.active/leg/ambulance_id.
ALTER TABLE routes
    ADD COLUMN IF NOT EXISTS progress_m          DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_eta_s          DOUBLE PRECISION;
ALTER TABLE ambulance_locations
    ADD COLUMN IF NOT EXISTS progress_m          DOUBLE PRECISION;
