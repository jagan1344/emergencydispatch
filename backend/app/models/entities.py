"""SQLAlchemy ORM mappings for the tables created by database/migrations/*.sql."""
from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geography, Geometry
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def point_wkt(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def linestring_wkt(coords: list[tuple[float, float]]) -> str:
    """coords as (lat, lon) pairs."""
    if len(coords) == 1:
        coords = [coords[0], coords[0]]
    return "SRID=4326;LINESTRING(" + ",".join(f"{lon} {lat}" for lat, lon in coords) + ")"


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceArea(Base):
    __tablename__ = "service_areas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    boundary = mapped_column(Geometry("POLYGON", srid=4326))


class RoadNode(Base):
    __tablename__ = "road_nodes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(Geography("POINT", srid=4326))


class RoadCondition(Base):
    __tablename__ = "road_conditions"
    road_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    highway_type: Mapped[str] = mapped_column(String(32))
    speed_limit_kph: Mapped[float] = mapped_column(Float)
    current_speed_kph: Mapped[float] = mapped_column(Float)
    congestion_level: Mapped[str] = mapped_column(String(16), default="FREE")
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    vehicle_density: Mapped[float] = mapped_column(Float, default=0.1)
    length_m: Mapped[float] = mapped_column(Float)
    oneway: Mapped[bool] = mapped_column(Boolean, default=False)
    geom = mapped_column(Geography("LINESTRING", srid=4326))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoadEdge(Base):
    __tablename__ = "road_edges"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    road_id: Mapped[str] = mapped_column(String(32), ForeignKey("road_conditions.road_id"))
    from_node: Mapped[int] = mapped_column(BigInteger)
    to_node: Mapped[int] = mapped_column(BigInteger)
    length_m: Mapped[float] = mapped_column(Float)


class Hospital(Base):
    __tablename__ = "hospitals"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(Geography("POINT", srid=4326))
    emergency_capacity: Mapped[int] = mapped_column(Integer)
    icu_available: Mapped[int] = mapped_column(Integer, default=0)
    trauma_available: Mapped[bool] = mapped_column(Boolean, default=False)
    cardiac_available: Mapped[bool] = mapped_column(Boolean, default=False)
    stroke_available: Mapped[bool] = mapped_column(Boolean, default=False)
    current_load: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmergencyIncident(Base):
    __tablename__ = "emergency_incidents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(16), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(Geography("POINT", srid=4326))
    address: Mapped[str | None] = mapped_column(String(255))
    emergency_type: Mapped[str] = mapped_column(String(16))
    patient_age: Mapped[int] = mapped_column(Integer)
    heart_rate: Mapped[int] = mapped_column(Integer)
    respiratory_rate: Mapped[int] = mapped_column(Integer)
    oxygen_saturation: Mapped[int | None] = mapped_column(Integer)
    consciousness: Mapped[str] = mapped_column(String(16))
    bleeding: Mapped[str] = mapped_column(String(16))
    injury_severity: Mapped[str] = mapped_column(String(16))
    accident_type: Mapped[str] = mapped_column(String(16))
    breathing_difficulty: Mapped[bool] = mapped_column(Boolean, default=False)
    chest_pain: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    rule_score: Mapped[float | None] = mapped_column(Float)
    rule_severity: Mapped[str | None] = mapped_column(String(16))
    rule_components: Mapped[dict | None] = mapped_column(JSONB)
    predicted_severity: Mapped[str | None] = mapped_column(String(16))
    ml_confidence: Mapped[float | None] = mapped_column(Float)
    ml_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    severity: Mapped[str | None] = mapped_column(String(16))
    severity_reasons: Mapped[list | None] = mapped_column(JSONB)
    priority: Mapped[float | None] = mapped_column(Float)
    priority_components: Mapped[dict | None] = mapped_column(JSONB)
    required_capability: Mapped[str | None] = mapped_column(String(16))
    assigned_ambulance: Mapped[str | None] = mapped_column(String(16), ForeignKey("ambulances.id"))
    destination_hospital: Mapped[str | None] = mapped_column(String(16), ForeignKey("hospitals.id"))
    status: Mapped[str] = mapped_column(String(16), default="CREATED")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hospital_arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="LIVE")
    historical_response_s: Mapped[float | None] = mapped_column(Float)
    historical_dispatch_s: Mapped[float | None] = mapped_column(Float)


class Ambulance(Base):
    __tablename__ = "ambulances"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    call_sign: Mapped[str] = mapped_column(String(32))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(Geography("POINT", srid=4326))
    base_latitude: Mapped[float] = mapped_column(Float)
    base_longitude: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="AVAILABLE")
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    equipment_level: Mapped[str] = mapped_column(String(16))
    driver_status: Mapped[str] = mapped_column(String(16), default="ON_DUTY")
    current_incident: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("emergency_incidents.id"))
    fuel_level: Mapped[float] = mapped_column(Float, default=100.0)
    current_speed: Mapped[float] = mapped_column(Float, default=0.0)
    destination: Mapped[str | None] = mapped_column(String(64))
    destination_lat: Mapped[float | None] = mapped_column(Float)
    destination_lon: Mapped[float | None] = mapped_column(Float)
    missions_today: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AmbulanceLocation(Base):
    __tablename__ = "ambulance_locations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ambulance_id: Mapped[str] = mapped_column(String(16), ForeignKey("ambulances.id"))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    location = mapped_column(Geography("POINT", srid=4326))
    speed_kph: Mapped[float] = mapped_column(Float, default=0)
    route_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Dispatch(Base):
    __tablename__ = "dispatches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("emergency_incidents.id"))
    ambulance_id: Mapped[str] = mapped_column(String(16), ForeignKey("ambulances.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    method: Mapped[str] = mapped_column(String(24))
    dispatch_score: Mapped[float] = mapped_column(Float)
    eta_to_patient_s: Mapped[float] = mapped_column(Float)
    distance_to_patient_m: Mapped[float] = mapped_column(Float)
    candidates: Mapped[list] = mapped_column(JSONB)
    explanation: Mapped[str] = mapped_column(Text)
    decision_ms: Mapped[float | None] = mapped_column(Float)
    hospital_id: Mapped[str | None] = mapped_column(String(16), ForeignKey("hospitals.id"))
    hospital_candidates: Mapped[list | None] = mapped_column(JSONB)
    hospital_explanation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class Route(Base):
    __tablename__ = "routes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("dispatches.id"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("emergency_incidents.id"))
    ambulance_id: Mapped[str | None] = mapped_column(String(16), ForeignKey("ambulances.id"))
    leg: Mapped[str] = mapped_column(String(16))
    engine: Mapped[str] = mapped_column(String(24))
    network_source: Mapped[str] = mapped_column(String(16))
    origin_lat: Mapped[float] = mapped_column(Float)
    origin_lon: Mapped[float] = mapped_column(Float)
    dest_lat: Mapped[float] = mapped_column(Float)
    dest_lon: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float] = mapped_column(Float)
    base_duration_s: Mapped[float] = mapped_column(Float)
    adjusted_duration_s: Mapped[float] = mapped_column(Float)
    osrm_duration_s: Mapped[float | None] = mapped_column(Float)
    shortest_distance_m: Mapped[float | None] = mapped_column(Float)
    geometry = mapped_column(Geography("LINESTRING", srid=4326))
    alternatives: Mapped[list | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reroute_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    reroute_reason: Mapped[str | None] = mapped_column(String(255))
    old_eta_s: Mapped[float | None] = mapped_column(Float)
    time_saved_s: Mapped[float | None] = mapped_column(Float)


class RouteSegment(Base):
    __tablename__ = "route_segments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    seq: Mapped[int] = mapped_column(Integer)
    road_id: Mapped[str | None] = mapped_column(String(32))
    from_node: Mapped[int | None] = mapped_column(BigInteger)
    to_node: Mapped[int | None] = mapped_column(BigInteger)
    length_m: Mapped[float] = mapped_column(Float)
    base_speed_kph: Mapped[float] = mapped_column(Float)
    planned_speed_kph: Mapped[float] = mapped_column(Float)
    cum_distance_m: Mapped[float] = mapped_column(Float)


class TrafficEvent(Base):
    __tablename__ = "traffic_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    road_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("road_conditions.road_id"))
    event_type: Mapped[str] = mapped_column(String(24))
    old_level: Mapped[str | None] = mapped_column(String(16))
    new_level: Mapped[str | None] = mapped_column(String(16))
    blocked: Mapped[bool | None] = mapped_column(Boolean)
    incident_multiplier: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))
    location = mapped_column(Geography("POINT", srid=4326))
    details: Mapped[dict | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IotMessage(Base):
    __tablename__ = "iot_messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    topic: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("emergency_incidents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    features: Mapped[dict] = mapped_column(JSONB)
    predicted_class: Mapped[str] = mapped_column(String(16))
    probabilities: Mapped[dict] = mapped_column(JSONB)
    latency_ms: Mapped[float | None] = mapped_column(Float)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_type: Mapped[str] = mapped_column(String(48))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ambulance_id: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
