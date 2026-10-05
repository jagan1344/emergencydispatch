"""Request / response validation models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

EmergencyType = Literal["accident", "cardiac", "respiratory", "trauma", "fire", "stroke", "other"]
Consciousness = Literal["ALERT", "VERBAL", "PAIN", "UNRESPONSIVE"]
Grade = Literal["NONE", "MINOR", "MODERATE", "SEVERE"]
AccidentType = Literal["NONE", "ROAD", "FALL", "FIRE", "INDUSTRIAL", "OTHER"]
Equipment = Literal["BASIC", "ADVANCED", "ICU"]
CongestionLevel = Literal["FREE", "LIGHT", "MODERATE", "HEAVY", "SEVERE", "BLOCKED"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=128)
    role: Literal["ADMIN", "DISPATCHER", "VIEWER"]


class CaseFeatures(BaseModel):
    emergency_type: EmergencyType
    patient_age: int = Field(ge=0, le=120)
    heart_rate: int = Field(ge=0, le=300, description="beats per minute")
    respiratory_rate: int = Field(ge=0, le=80, description="breaths per minute")
    oxygen_saturation: int | None = Field(default=None, ge=50, le=100, description="SpO2 %")
    consciousness: Consciousness
    bleeding: Grade
    injury_severity: Grade
    accident_type: AccidentType = "NONE"
    breathing_difficulty: bool = False
    chest_pain: bool = False


class EmergencyCreate(CaseFeatures):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)
    auto_dispatch: bool | None = None

    @field_validator("latitude", "longitude")
    @classmethod
    def finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("coordinate must be finite")
        return v


class DispatchRequest(BaseModel):
    ambulance_id: str | None = Field(default=None, description="optional manual override")


class Point(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    origin: Point
    destination: Point


class TrafficEventCreate(BaseModel):
    event_type: Literal["ACCIDENT", "BLOCK", "UNBLOCK", "CONGESTION", "CLEAR"]
    road_id: str | None = Field(default=None, max_length=32)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    level: CongestionLevel | None = None
    radius_m: float = Field(default=60, gt=0, le=1000)


class TrafficSimulateRequest(BaseModel):
    steps: int = Field(default=1, ge=1, le=50)
    changes_per_step: int = Field(default=5, ge=1, le=100)
    seed: int | None = None


class AmbulanceCreate(BaseModel):
    id: str = Field(pattern=r"^AMB-\d{3,6}$")
    call_sign: str = Field(max_length=32)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    equipment_level: Equipment
    capacity: int = Field(default=1, ge=1, le=4)
    fuel_level: float = Field(default=100, ge=0, le=100)


class AmbulanceUpdate(BaseModel):
    status: Literal["AVAILABLE", "MAINTENANCE", "OFFLINE"] | None = None
    equipment_level: Equipment | None = None
    driver_status: Literal["ON_DUTY", "OFF_DUTY", "BREAK"] | None = None
    fuel_level: float | None = Field(default=None, ge=0, le=100)


class HospitalCreate(BaseModel):
    id: str = Field(pattern=r"^HSP-\d{3,6}$")
    name: str = Field(max_length=128)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    emergency_capacity: int = Field(gt=0, le=1000)
    icu_available: int = Field(default=0, ge=0, le=500)
    trauma_available: bool = False
    cardiac_available: bool = False
    stroke_available: bool = False


class HospitalUpdate(BaseModel):
    emergency_capacity: int | None = Field(default=None, gt=0, le=1000)
    icu_available: int | None = Field(default=None, ge=0, le=500)
    trauma_available: bool | None = None
    cardiac_available: bool | None = None
    stroke_available: bool | None = None
    current_load: int | None = Field(default=None, ge=0)
    status: Literal["ACTIVE", "DIVERT", "CLOSED"] | None = None


class SimulationStart(BaseModel):
    seed: int = 42
    ambulances: int = Field(default=10, ge=1, le=200)
    hospitals: int = Field(default=5, ge=1, le=50)
    incidents: int = Field(default=20, ge=0, le=1000)
    traffic_events: int = Field(default=30, ge=0, le=2000)
    duration_s: float = Field(default=180, ge=10, le=7200, description="wall-clock seconds over which events are spread")


class DemoScenarioRequest(BaseModel):
    seed: int = 42
