-- 0004_decision_intelligence.sql
-- Confidence-aware dispatch, explainability/counterfactuals, predictive traffic, predictive hospital
-- congestion, resource reallocation and prediction-aware routing.

-- (1) confidence-aware decision recorded on the incident
ALTER TABLE emergency_incidents
    ADD COLUMN IF NOT EXISTS class_probabilities JSONB,
    ADD COLUMN IF NOT EXISTS confidence_level   VARCHAR(16),      -- HIGH / MEDIUM / LOW / UNAVAILABLE
    ADD COLUMN IF NOT EXISTS decision_mode      VARCHAR(24),      -- AUTO_DISPATCH / DISPATCH_WITH_REVIEW / HUMAN_REVIEW / HUMAN_APPROVED
    ADD COLUMN IF NOT EXISTS decision_reason    VARCHAR(500),
    ADD COLUMN IF NOT EXISTS reviewed_by        VARCHAR(64),
    ADD COLUMN IF NOT EXISTS reviewed_at        TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_incidents_decision_mode ON emergency_incidents (decision_mode) WHERE status = 'WAITING';

-- (2) explainability: counterfactuals stored with the decision; a dispatch can now be reallocated
ALTER TABLE dispatches
    ADD COLUMN IF NOT EXISTS counterfactuals JSONB,
    ADD COLUMN IF NOT EXISTS decision_mode   VARCHAR(24);
ALTER TABLE dispatches DROP CONSTRAINT IF EXISTS dispatches_status_check;
ALTER TABLE dispatches ADD CONSTRAINT dispatches_status_check
    CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED','REALLOCATED'));

-- prediction-aware routing: ETA under predicted traffic at planning time
ALTER TABLE routes
    ADD COLUMN IF NOT EXISTS predicted_duration_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS prediction_horizon_min DOUBLE PRECISION;

-- (3) latest traffic prediction per road and horizon (upserted by the prediction service)
CREATE TABLE IF NOT EXISTS traffic_predictions (
    road_id             VARCHAR(32) NOT NULL REFERENCES road_conditions(road_id) ON DELETE CASCADE,
    horizon_min         DOUBLE PRECISION NOT NULL,          -- simulated minutes ahead
    prediction_time     TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_level       VARCHAR(16) NOT NULL,
    predicted_level     VARCHAR(16) NOT NULL,
    current_speed_kph   DOUBLE PRECISION NOT NULL,
    predicted_speed_kph DOUBLE PRECISION NOT NULL,
    predicted_delay_s   DOUBLE PRECISION NOT NULL,          -- extra traversal time vs current speed (+ = slower)
    confidence          DOUBLE PRECISION NOT NULL,
    method              VARCHAR(24) NOT NULL,               -- MODEL / FALLBACK
    model_version       VARCHAR(64) NOT NULL,
    PRIMARY KEY (road_id, horizon_min)
);
CREATE INDEX IF NOT EXISTS ix_traffic_predictions_time ON traffic_predictions (prediction_time);

-- (4) latest hospital congestion prediction per hospital and horizon
CREATE TABLE IF NOT EXISTS hospital_predictions (
    hospital_id         VARCHAR(16) NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    horizon_min         DOUBLE PRECISION NOT NULL,
    prediction_time     TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_load        INTEGER NOT NULL,
    capacity            INTEGER NOT NULL,
    incoming_ambulances INTEGER NOT NULL,
    predicted_load      DOUBLE PRECISION NOT NULL,
    predicted_load_pct  DOUBLE PRECISION NOT NULL,
    expected_wait_min   DOUBLE PRECISION NOT NULL,          -- ESTIMATED (queueing approximation)
    arrival_rate_per_h  DOUBLE PRECISION NOT NULL,
    mean_stay_min       DOUBLE PRECISION NOT NULL,
    method              VARCHAR(24) NOT NULL,               -- OBSERVED_RATES / DEFAULT_RATES
    data_points         INTEGER NOT NULL,
    confidence          VARCHAR(16) NOT NULL,
    model_version       VARCHAR(64) NOT NULL,
    PRIMARY KEY (hospital_id, horizon_min)
);

-- (5) competing emergencies: detected conflicts, reallocations and escalations
CREATE TABLE IF NOT EXISTS resource_conflicts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ambulance_id        VARCHAR(16) REFERENCES ambulances(id),
    from_incident_id    UUID REFERENCES emergency_incidents(id) ON DELETE CASCADE,  -- holder / lower priority
    to_incident_id      UUID REFERENCES emergency_incidents(id) ON DELETE CASCADE,  -- requester
    kind                VARCHAR(24) NOT NULL,      -- CONTESTED_UNIT / REALLOCATION
    decision            VARCHAR(24) NOT NULL,      -- KEEP_CURRENT / ASSIGN_OTHER / REALLOCATED / ESCALATED / APPROVED / REJECTED
    reason              TEXT NOT NULL,
    from_eta_before_s   DOUBLE PRECISION,
    from_eta_after_s    DOUBLE PRECISION,          -- donor ETA with its replacement unit (impact = after - before)
    to_eta_s            DOUBLE PRECISION,
    alternative_eta_s   DOUBLE PRECISION,          -- best ETA the requester had without reallocation
    details             JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolved_by         VARCHAR(64),
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_conflicts_created ON resource_conflicts (created_at);
CREATE INDEX IF NOT EXISTS ix_conflicts_to ON resource_conflicts (to_incident_id);
CREATE INDEX IF NOT EXISTS ix_conflicts_from ON resource_conflicts (from_incident_id);
CREATE INDEX IF NOT EXISTS ix_conflicts_open ON resource_conflicts (decision) WHERE decision = 'ESCALATED';
