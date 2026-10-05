-- 0002_real_data.sql : provenance + real-device support
-- Where each hospital record came from: SYNTHETIC (seed), OSM (OpenStreetMap import), VERIFIED (user CSV override)
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS data_source VARCHAR(16) NOT NULL DEFAULT 'SYNTHETIC';
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS osm_id VARCHAR(32);
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS phone VARCHAR(64);
ALTER TABLE hospitals ALTER COLUMN name TYPE VARCHAR(255);

-- SIMULATED: moved by the MQTT simulator; DEVICE: position comes from a real phone/GPS device (crew app)
ALTER TABLE ambulances ADD COLUMN IF NOT EXISTS gps_source VARCHAR(16) NOT NULL DEFAULT 'SIMULATED'
    CHECK (gps_source IN ('SIMULATED', 'DEVICE'));
ALTER TABLE ambulances ADD COLUMN IF NOT EXISTS gps_accuracy_m DOUBLE PRECISION;

-- extra vital signs available in real triage datasets (optional at intake)
ALTER TABLE emergency_incidents ADD COLUMN IF NOT EXISTS systolic_bp INTEGER CHECK (systolic_bp BETWEEN 30 AND 300);
ALTER TABLE emergency_incidents ADD COLUMN IF NOT EXISTS temperature_c DOUBLE PRECISION CHECK (temperature_c BETWEEN 25 AND 45);
