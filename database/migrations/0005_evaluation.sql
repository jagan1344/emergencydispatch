-- 0005_evaluation.sql
-- Research evaluation layer: experiment runs, the replayed scenarios and per-(scenario, strategy) results.
-- Only NEW tables; operational tables (incidents, ambulances, routes, ...) are never written by experiments.

CREATE TABLE IF NOT EXISTS experiment_runs (
    id              UUID PRIMARY KEY,
    run_name        VARCHAR(80)  NOT NULL UNIQUE,
    kind            VARCHAR(24)  NOT NULL,                 -- PROGRESSIVE / ABLATION / CUSTOM / MIXED
    strategies      JSONB        NOT NULL,                 -- ordered list of strategy names
    configuration   JSONB        NOT NULL,                 -- capability flags per strategy + simulation params
    scenario_count  INTEGER      NOT NULL CHECK (scenario_count > 0),
    random_seed     BIGINT       NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')),
    metadata        JSONB,                                 -- git SHA, model versions, routing source, city, settings
    summary         JSONB,                                 -- comparison table + statistics (filled when finished)
    output_dir      VARCHAR(400),
    error           TEXT,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS experiment_scenarios (
    id                 UUID PRIMARY KEY,
    experiment_run_id  UUID NOT NULL REFERENCES experiment_runs(id) ON DELETE CASCADE,
    scenario_number    INTEGER NOT NULL,
    random_seed        BIGINT  NOT NULL,
    fingerprint        VARCHAR(32) NOT NULL,               -- hash of the full definition (reproducibility check)
    summary            JSONB   NOT NULL,                   -- counts: calls, fleet, regimes, severities, events
    definition         JSONB   NOT NULL,                   -- complete scenario (patients, fleet, hospitals, traffic, events)
    UNIQUE (experiment_run_id, scenario_number)
);

CREATE TABLE IF NOT EXISTS experiment_results (
    id                        UUID PRIMARY KEY,
    experiment_run_id         UUID NOT NULL REFERENCES experiment_runs(id) ON DELETE CASCADE,
    scenario_id               UUID NOT NULL REFERENCES experiment_scenarios(id) ON DELETE CASCADE,
    strategy                  VARCHAR(40) NOT NULL,
    status                    VARCHAR(16) NOT NULL CHECK (status IN ('OK', 'FAILED')),
    error                     TEXT,
    response_time_s           DOUBLE PRECISION,
    patient_wait_s            DOUBLE PRECISION,
    initial_eta_s             DOUBLE PRECISION,
    actual_travel_s           DOUBLE PRECISION,
    reroute_count             INTEGER,
    reroute_improvement_s     DOUBLE PRECISION,
    ambulance_utilization     DOUBLE PRECISION,
    hospital_wait_s           DOUBLE PRECISION,           -- simulated ED wait (queue model on simulated load)
    critical_delay_s          DOUBLE PRECISION,
    resource_conflicts        INTEGER,
    manual_interventions      INTEGER,
    automatic_decision_rate   DOUBLE PRECISION,
    metrics                   JSONB,                      -- every scenario metric (see app/evaluation/metrics.py)
    incidents                 JSONB,                      -- per-call records
    runtime_ms                DOUBLE PRECISION,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_run_id, scenario_id, strategy)
);
CREATE INDEX IF NOT EXISTS ix_experiment_results_run ON experiment_results (experiment_run_id, strategy);
