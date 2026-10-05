-- 0001_initial.sql : core schema for the AI Emergency Dispatch system (PostgreSQL 13+ / PostGIS 3)
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(64) NOT NULL UNIQUE,
    full_name     VARCHAR(128),
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(16) NOT NULL CHECK (role IN ('ADMIN','DISPATCHER','VIEWER')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Service area polygon used to validate that incidents lie inside the operating region (ST_Contains).
CREATE TABLE service_areas (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(128) NOT NULL,
    boundary GEOMETRY(POLYGON, 4326) NOT NULL
);
CREATE INDEX ix_service_areas_boundary ON service_areas USING GIST (boundary);

-- Road network graph (imported from OpenStreetMap, or a synthetic grid in demo-fallback mode).
CREATE TABLE road_nodes (
    id        BIGINT PRIMARY KEY,          -- OSM node id (synthetic ids are negative)
    latitude  DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    location  GEOGRAPHY(POINT, 4326) NOT NULL
);
CREATE INDEX ix_road_nodes_location ON road_nodes USING GIST (location);

-- One row per road (an OSM way inside the service area). This is the unit the traffic model acts on.
CREATE TABLE road_conditions (
    road_id             VARCHAR(32) PRIMARY KEY,
    name                VARCHAR(255),
    highway_type        VARCHAR(32) NOT NULL,
    speed_limit_kph     DOUBLE PRECISION NOT NULL,
    current_speed_kph   DOUBLE PRECISION NOT NULL,
    congestion_level    VARCHAR(16) NOT NULL DEFAULT 'FREE'
                        CHECK (congestion_level IN ('FREE','LIGHT','MODERATE','HEAVY','SEVERE','BLOCKED')),
    blocked             BOOLEAN NOT NULL DEFAULT FALSE,
    incident_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (incident_multiplier > 0 AND incident_multiplier <= 1),
    vehicle_density     DOUBLE PRECISION NOT NULL DEFAULT 0.1,  -- vehicles per metre-lane (simulated)
    length_m            DOUBLE PRECISION NOT NULL,
    oneway              BOOLEAN NOT NULL DEFAULT FALSE,
    geom                GEOGRAPHY(LINESTRING, 4326) NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_road_conditions_geom ON road_conditions USING GIST (geom);
CREATE INDEX ix_road_conditions_level ON road_conditions (congestion_level);

-- Directed graph edges between consecutive OSM nodes; each edge belongs to one road.
CREATE TABLE road_edges (
    id        BIGSERIAL PRIMARY KEY,
    road_id   VARCHAR(32) NOT NULL REFERENCES road_conditions(road_id) ON DELETE CASCADE,
    from_node BIGINT NOT NULL REFERENCES road_nodes(id) ON DELETE CASCADE,
    to_node   BIGINT NOT NULL REFERENCES road_nodes(id) ON DELETE CASCADE,
    length_m  DOUBLE PRECISION NOT NULL CHECK (length_m >= 0)
);
CREATE INDEX ix_road_edges_road ON road_edges (road_id);

CREATE TABLE hospitals (
    id                 VARCHAR(16) PRIMARY KEY,
    name               VARCHAR(128) NOT NULL,
    latitude           DOUBLE PRECISION NOT NULL,
    longitude          DOUBLE PRECISION NOT NULL,
    location           GEOGRAPHY(POINT, 4326) NOT NULL,
    emergency_capacity INTEGER NOT NULL CHECK (emergency_capacity > 0),
    icu_available      INTEGER NOT NULL DEFAULT 0 CHECK (icu_available >= 0),
    trauma_available   BOOLEAN NOT NULL DEFAULT FALSE,
    cardiac_available  BOOLEAN NOT NULL DEFAULT FALSE,
    stroke_available   BOOLEAN NOT NULL DEFAULT FALSE,
    current_load       INTEGER NOT NULL DEFAULT 0 CHECK (current_load >= 0),
    status             VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DIVERT','CLOSED')),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_hospitals_location ON hospitals USING GIST (location);

CREATE TABLE emergency_incidents (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference            VARCHAR(16) NOT NULL UNIQUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    latitude             DOUBLE PRECISION NOT NULL,
    longitude            DOUBLE PRECISION NOT NULL,
    location             GEOGRAPHY(POINT, 4326) NOT NULL,
    address              VARCHAR(255),
    emergency_type       VARCHAR(16) NOT NULL CHECK (emergency_type IN ('accident','cardiac','respiratory','trauma','fire','stroke','other')),
    patient_age          INTEGER NOT NULL CHECK (patient_age BETWEEN 0 AND 120),
    heart_rate           INTEGER NOT NULL CHECK (heart_rate BETWEEN 0 AND 300),
    respiratory_rate     INTEGER NOT NULL CHECK (respiratory_rate BETWEEN 0 AND 80),
    oxygen_saturation    INTEGER CHECK (oxygen_saturation BETWEEN 50 AND 100),
    consciousness        VARCHAR(16) NOT NULL,   -- AVPU scale: ALERT/VERBAL/PAIN/UNRESPONSIVE
    bleeding             VARCHAR(16) NOT NULL,   -- NONE/MINOR/MODERATE/SEVERE
    injury_severity      VARCHAR(16) NOT NULL,   -- NONE/MINOR/MODERATE/SEVERE
    accident_type        VARCHAR(16) NOT NULL,   -- NONE/ROAD/FALL/FIRE/INDUSTRIAL/OTHER
    breathing_difficulty BOOLEAN NOT NULL DEFAULT FALSE,
    chest_pain           BOOLEAN NOT NULL DEFAULT FALSE,
    notes                TEXT,
    rule_score           DOUBLE PRECISION,
    rule_severity        VARCHAR(16),
    rule_components      JSONB,
    predicted_severity   VARCHAR(16),
    ml_confidence        DOUBLE PRECISION,
    ml_status            VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- OK / UNAVAILABLE / PENDING
    severity             VARCHAR(16),            -- final severity used by the dispatcher
    severity_reasons     JSONB,
    priority             DOUBLE PRECISION,       -- PriorityScore 0..100
    priority_components  JSONB,
    required_capability  VARCHAR(16),
    assigned_ambulance   VARCHAR(16),
    destination_hospital VARCHAR(16),
    status               VARCHAR(16) NOT NULL DEFAULT 'CREATED'
                         CHECK (status IN ('CREATED','CLASSIFYING','WAITING','DISPATCHED','EN_ROUTE','ARRIVED',
                                           'PATIENT_LOADED','TO_HOSPITAL','COMPLETED','CANCELLED')),
    dispatched_at        TIMESTAMPTZ,
    arrived_at           TIMESTAMPTZ,
    loaded_at            TIMESTAMPTZ,
    hospital_arrived_at  TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    cancelled_at         TIMESTAMPTZ,
    created_by           VARCHAR(64),
    source               VARCHAR(16) NOT NULL DEFAULT 'LIVE',     -- LIVE / SIMULATION / SCENARIO / HISTORICAL_SEED
    historical_response_s DOUBLE PRECISION,  -- only for HISTORICAL_SEED rows (computed from graph ETA at seed time)
    historical_dispatch_s DOUBLE PRECISION
);
CREATE INDEX ix_incidents_status ON emergency_incidents (status);
CREATE INDEX ix_incidents_created ON emergency_incidents (created_at);
CREATE INDEX ix_incidents_location ON emergency_incidents USING GIST (location);

CREATE TABLE ambulances (
    id               VARCHAR(16) PRIMARY KEY,
    call_sign        VARCHAR(32) NOT NULL,
    latitude         DOUBLE PRECISION NOT NULL,
    longitude        DOUBLE PRECISION NOT NULL,
    location         GEOGRAPHY(POINT, 4326) NOT NULL,
    base_latitude    DOUBLE PRECISION NOT NULL,
    base_longitude   DOUBLE PRECISION NOT NULL,
    status           VARCHAR(24) NOT NULL DEFAULT 'AVAILABLE'
                     CHECK (status IN ('AVAILABLE','DISPATCHED','EN_ROUTE_TO_PATIENT','AT_SCENE','TRANSPORTING',
                                       'AT_HOSPITAL','MAINTENANCE','OFFLINE')),
    capacity         INTEGER NOT NULL DEFAULT 1,
    equipment_level  VARCHAR(16) NOT NULL CHECK (equipment_level IN ('BASIC','ADVANCED','ICU')),
    driver_status    VARCHAR(16) NOT NULL DEFAULT 'ON_DUTY',
    current_incident UUID REFERENCES emergency_incidents(id) ON DELETE SET NULL,
    fuel_level       DOUBLE PRECISION NOT NULL DEFAULT 100 CHECK (fuel_level BETWEEN 0 AND 100),
    current_speed    DOUBLE PRECISION NOT NULL DEFAULT 0,      -- km/h
    destination      VARCHAR(64),
    destination_lat  DOUBLE PRECISION,
    destination_lon  DOUBLE PRECISION,
    missions_today   INTEGER NOT NULL DEFAULT 0,
    last_updated     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ambulances_status ON ambulances (status);
CREATE INDEX ix_ambulances_location ON ambulances USING GIST (location);

ALTER TABLE emergency_incidents
    ADD CONSTRAINT fk_incident_ambulance FOREIGN KEY (assigned_ambulance) REFERENCES ambulances(id) ON DELETE SET NULL,
    ADD CONSTRAINT fk_incident_hospital FOREIGN KEY (destination_hospital) REFERENCES hospitals(id) ON DELETE SET NULL;

CREATE TABLE ambulance_locations (
    id           BIGSERIAL PRIMARY KEY,
    ambulance_id VARCHAR(16) NOT NULL REFERENCES ambulances(id) ON DELETE CASCADE,
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    location     GEOGRAPHY(POINT, 4326) NOT NULL,
    speed_kph    DOUBLE PRECISION NOT NULL DEFAULT 0,
    route_id     UUID,
    recorded_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_amb_locations_amb_time ON ambulance_locations (ambulance_id, recorded_at DESC);

CREATE TABLE dispatches (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id           UUID NOT NULL REFERENCES emergency_incidents(id) ON DELETE CASCADE,
    ambulance_id          VARCHAR(16) NOT NULL REFERENCES ambulances(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    method                VARCHAR(24) NOT NULL,             -- WEIGHTED_SCORE / ORTOOLS_ASSIGNMENT
    dispatch_score        DOUBLE PRECISION NOT NULL,
    eta_to_patient_s      DOUBLE PRECISION NOT NULL,
    distance_to_patient_m DOUBLE PRECISION NOT NULL,
    candidates            JSONB NOT NULL,                    -- every evaluated candidate with score components
    explanation           TEXT NOT NULL,
    decision_ms           DOUBLE PRECISION,
    hospital_id           VARCHAR(16) REFERENCES hospitals(id),
    hospital_candidates   JSONB,
    hospital_explanation  TEXT,
    status                VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED'))
);
CREATE INDEX ix_dispatches_incident ON dispatches (incident_id);
CREATE INDEX ix_dispatches_created ON dispatches (created_at);

CREATE TABLE routes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id           UUID REFERENCES dispatches(id) ON DELETE CASCADE,
    incident_id           UUID REFERENCES emergency_incidents(id) ON DELETE CASCADE,
    ambulance_id          VARCHAR(16) REFERENCES ambulances(id),
    leg                   VARCHAR(16) NOT NULL CHECK (leg IN ('TO_PATIENT','TO_HOSPITAL','PREVIEW')),
    engine                VARCHAR(24) NOT NULL,               -- osrm / graph
    network_source        VARCHAR(16) NOT NULL,               -- osm / synthetic
    origin_lat            DOUBLE PRECISION NOT NULL,
    origin_lon            DOUBLE PRECISION NOT NULL,
    dest_lat              DOUBLE PRECISION NOT NULL,
    dest_lon              DOUBLE PRECISION NOT NULL,
    distance_m            DOUBLE PRECISION NOT NULL,
    base_duration_s       DOUBLE PRECISION NOT NULL,          -- free-flow (speed limits)
    adjusted_duration_s   DOUBLE PRECISION NOT NULL,          -- traffic-adjusted ETA at planning time
    osrm_duration_s       DOUBLE PRECISION,
    shortest_distance_m   DOUBLE PRECISION,                   -- shortest possible network distance
    geometry              GEOGRAPHY(LINESTRING, 4326) NOT NULL,
    alternatives          JSONB,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at         TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    reroute_of            UUID REFERENCES routes(id),
    reroute_reason        VARCHAR(255),
    old_eta_s             DOUBLE PRECISION,
    time_saved_s          DOUBLE PRECISION
);
CREATE INDEX ix_routes_active ON routes (active) WHERE active;
CREATE INDEX ix_routes_incident ON routes (incident_id);
CREATE INDEX ix_routes_geometry ON routes USING GIST (geometry);

CREATE TABLE route_segments (
    id             BIGSERIAL PRIMARY KEY,
    route_id       UUID NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    road_id        VARCHAR(32),
    from_node      BIGINT,
    to_node        BIGINT,
    length_m       DOUBLE PRECISION NOT NULL,
    base_speed_kph DOUBLE PRECISION NOT NULL,
    planned_speed_kph DOUBLE PRECISION NOT NULL,     -- traffic-adjusted speed when the route was planned
    cum_distance_m DOUBLE PRECISION NOT NULL
);
CREATE INDEX ix_route_segments_route ON route_segments (route_id, seq);
CREATE INDEX ix_route_segments_road ON route_segments (road_id);

CREATE TABLE traffic_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    road_id           VARCHAR(32) REFERENCES road_conditions(road_id) ON DELETE CASCADE,
    event_type        VARCHAR(24) NOT NULL,   -- ACCIDENT / BLOCK / UNBLOCK / CONGESTION / CLEAR / DENSITY
    old_level         VARCHAR(16),
    new_level         VARCHAR(16),
    blocked           BOOLEAN,
    incident_multiplier DOUBLE PRECISION,
    source            VARCHAR(16) NOT NULL,   -- SIMULATOR / DISPATCHER / SCENARIO / SIMULATION
    location          GEOGRAPHY(POINT, 4326),
    details           JSONB,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    cleared_at        TIMESTAMPTZ
);
CREATE INDEX ix_traffic_events_road ON traffic_events (road_id);
CREATE INDEX ix_traffic_events_created ON traffic_events (created_at);

CREATE TABLE iot_messages (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    topic       VARCHAR(128) NOT NULL,
    payload     JSONB NOT NULL
);
CREATE INDEX ix_iot_messages_time ON iot_messages (received_at);
CREATE INDEX ix_iot_messages_topic ON iot_messages (topic);

CREATE TABLE model_predictions (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     UUID REFERENCES emergency_incidents(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_name      VARCHAR(64) NOT NULL,
    model_version   VARCHAR(64) NOT NULL,
    features        JSONB NOT NULL,
    predicted_class VARCHAR(16) NOT NULL,
    probabilities   JSONB NOT NULL,
    latency_ms      DOUBLE PRECISION
);
CREATE INDEX ix_model_predictions_incident ON model_predictions (incident_id);

CREATE TABLE system_events (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type   VARCHAR(48) NOT NULL,
    incident_id  UUID,
    ambulance_id VARCHAR(16),
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_system_events_type_time ON system_events (event_type, created_at);
CREATE INDEX ix_system_events_incident ON system_events (incident_id);

CREATE SEQUENCE incident_reference_seq START 1;
