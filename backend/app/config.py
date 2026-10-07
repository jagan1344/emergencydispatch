"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ems"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ems_test"
    redis_url: str = ""

    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883

    osrm_url: str = ""
    osrm_timeout_s: float = 3.0

    jwt_secret: str = ""
    jwt_expire_minutes: int = 480
    frontend_url: str = "http://localhost:5173"

    city_name: str = "Bengaluru"
    city_lat: float = 12.9716
    city_lon: float = 77.5946
    city_radius_m: float = 6000
    osm_pbf_path: str = str(PROJECT_DIR / "data" / "maps" / "bengaluru.osm.pbf")

    # optional CSV with verified hospital capabilities (applied by the seed; see app/hospital_data.py)
    hospitals_csv: str = ""

    sim_time_scale: float = 4.0
    scene_time_s: float = 120.0
    handover_time_s: float = 90.0
    auto_dispatch: bool = True
    # Route is recalculated when the remaining ETA grows by more than this fraction.
    reroute_threshold: float = 0.20
    max_dispatch_candidates: int = 8

    # ---- (1) confidence-aware dispatch: ML top-class probability thresholds
    dispatch_confidence_high: float = 0.75     # >= -> AUTO_DISPATCH
    dispatch_confidence_low: float = 0.50      # < -> HUMAN_REVIEW (between: DISPATCH_WITH_REVIEW)
    # a HUMAN_REVIEW incident is auto-dispatched after this many simulated seconds (with the more severe of
    # ML / rule severity) so that a missing human never strands a patient; 0 disables the safety net
    human_review_timeout_s: float = 120.0

    # ---- (3) predictive traffic
    traffic_prediction_enabled: bool = True
    traffic_prediction_horizon_min: float = 10.0     # simulated minutes ahead
    traffic_prediction_interval_s: float = 30.0      # wall seconds between prediction cycles
    traffic_model_retrain_s: float = 600.0           # wall seconds between model re-training
    traffic_model_min_samples: int = 300             # below this -> transparent fallback model

    # ---- (4) predictive hospital congestion (estimates, clearly labelled)
    hospital_prediction_horizon_min: float = 10.0
    hospital_prediction_interval_s: float = 30.0
    hospital_rate_window_min: float = 240.0          # simulated minutes of history used for observed rates
    ed_mean_stay_min: float = 240.0                  # default ED length of stay when no discharges observed
    ed_treatment_slot_min: float = 5.0               # mean service time per patient for the wait estimate
    hospital_max_wait_min: float = 120.0

    # ---- (5) resource reallocation between competing emergencies
    reallocation_enabled: bool = True
    # consider reallocation if best free unit ETA exceeds this
    resource_reallocation_threshold_s: float = Field(480.0, validation_alias=AliasChoices("RESOURCE_REALLOCATION_THRESHOLD", "resource_reallocation_threshold_s"))
    reallocation_min_gain_s: float = 120.0             # diverted unit must arrive at least this much sooner
    reallocation_max_donor_delay_s: float = 300.0      # max extra delay for the donor incident, else ESCALATE
    reallocation_priority_margin: float = 10.0         # requester priority must exceed donor priority by this

    # ---- (6) telemetry-driven re-routing
    # simulated seconds
    reroute_min_eta_savings_s: float = Field(20.0, validation_alias=AliasChoices("REROUTE_MIN_ETA_SAVINGS", "reroute_min_eta_savings_s"))
    reroute_min_improvement_percent: float = 5.0
    # simulated seconds between re-routes of one ambulance
    reroute_cooldown_s: float = Field(90.0, validation_alias=AliasChoices("REROUTE_COOLDOWN_SECONDS", "reroute_cooldown_s"))
    reroute_oscillation_similarity: float = 0.8      # reject routes this similar to a recently abandoned one
    route_monitor_interval_s: float = 5.0            # wall seconds
    # when every route crosses a closed road: false (default) = ROUTE_UNAVAILABLE for the dispatcher;
    # true = legacy last-resort access through the closure at walking pace (e.g. police-escorted)
    closure_access_fallback: bool = False
    # route checkpoint / restart recovery: checkpoint at most every N wall seconds per route; a GPS fix farther than
    # the threshold from the checkpointed route position triggers a re-plan from the GPS fix instead of a resume
    route_checkpoint_interval_s: float = 2.0
    route_checkpoint_replan_threshold_m: float = 75.0
    # severity probability calibration (used only if training showed it improves held-out Brier and ECE)
    calibration_enabled: bool = True
    # default seed of research experiments (CLI --seed / API) - reproducibility
    evaluation_seed: int = 42

    log_level: str = "INFO"
    background_tasks: bool = True

    def resolved_jwt_secret(self) -> str:
        return self.jwt_secret or _EPHEMERAL_SECRET


_EPHEMERAL_SECRET = secrets.token_hex(32)


@lru_cache
def get_settings() -> Settings:
    return Settings()
