"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

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

    sim_time_scale: float = 4.0
    scene_time_s: float = 120.0
    handover_time_s: float = 90.0
    auto_dispatch: bool = True
    # Route is recalculated when the remaining ETA grows by more than this fraction.
    reroute_threshold: float = 0.20
    max_dispatch_candidates: int = 8

    log_level: str = "INFO"
    background_tasks: bool = True

    def resolved_jwt_secret(self) -> str:
        return self.jwt_secret or _EPHEMERAL_SECRET


_EPHEMERAL_SECRET = secrets.token_hex(32)


@lru_cache
def get_settings() -> Settings:
    return Settings()
