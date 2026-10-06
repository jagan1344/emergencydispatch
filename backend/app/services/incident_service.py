"""Emergency intake: validation → ML severity → rule score → priority → WAITING queue."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.confidence import assess
from app.dispatch.priority import priority_score
from app.dispatch.severity import combine_severity, required_capability, severity_score
from app.ml.predict import ModelUnavailable
from app.models import Ambulance, EmergencyIncident, ModelPrediction
from app.models.entities import point_wkt
from app.mqtt.client import publish
from app.services import metrics
from app.services.events import after_commit, emit
from app.services.state import STATE
from app.utils.logging import log_event
from app.utils.timeutil import sim_seconds, utcnow

log = logging.getLogger("app.incidents")
OPEN_STATUSES = ("CREATED", "CLASSIFYING", "WAITING", "DISPATCHED", "EN_ROUTE", "ARRIVED", "PATIENT_LOADED",
                 "TO_HOSPITAL")
CAPABLE = {"BASIC": ("BASIC", "ADVANCED", "ICU"), "ADVANCED": ("ADVANCED", "ICU"), "ICU": ("ICU",)}


class IncidentValidationError(ValueError):
    pass


def inside_service_area(db: Session, lat: float, lon: float) -> bool | None:
    """ST_Contains test against the configured service-area polygon (None when no area is configured)."""
    row = db.execute(text(
        "SELECT bool_or(ST_Contains(boundary, ST_SetSRID(ST_MakePoint(:lon,:lat),4326))), count(*) FROM service_areas"),
        {"lat": lat, "lon": lon}).first()
    if not row or row[1] == 0:
        return None
    return bool(row[0])


MAX_ROAD_DISTANCE_M = 1000.0


def distance_to_road_m(db: Session, lat: float, lon: float) -> float | None:
    """Distance from a point to the nearest road of the imported network (PostGIS KNN + ST_Distance)."""
    row = db.execute(text(
        "SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography) FROM road_conditions "
        "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography LIMIT 1"), {"lat": lat, "lon": lon}).first()
    return None if row is None else float(row[0])


def incident_dict(i: EmergencyIncident) -> dict:
    return {
        "id": str(i.id), "reference": i.reference, "created_at": i.created_at, "latitude": i.latitude,
        "longitude": i.longitude, "address": i.address, "emergency_type": i.emergency_type,
        "patient_age": i.patient_age, "heart_rate": i.heart_rate, "respiratory_rate": i.respiratory_rate,
        "oxygen_saturation": i.oxygen_saturation, "systolic_bp": i.systolic_bp, "temperature_c": i.temperature_c,
        "consciousness": i.consciousness, "bleeding": i.bleeding,
        "injury_severity": i.injury_severity, "accident_type": i.accident_type,
        "breathing_difficulty": i.breathing_difficulty, "chest_pain": i.chest_pain, "notes": i.notes,
        "rule_score": i.rule_score, "rule_severity": i.rule_severity, "rule_components": i.rule_components,
        "predicted_severity": i.predicted_severity, "ml_confidence": i.ml_confidence, "ml_status": i.ml_status,
        "severity": i.severity, "severity_reasons": i.severity_reasons, "priority": i.priority,
        "priority_components": i.priority_components, "required_capability": i.required_capability,
        "assigned_ambulance": i.assigned_ambulance, "destination_hospital": i.destination_hospital,
        "status": i.status, "dispatch_note": i.dispatch_note, "class_probabilities": i.class_probabilities,
        "confidence_level": i.confidence_level, "decision_mode": i.decision_mode, "decision_reason": i.decision_reason,
        "reviewed_by": i.reviewed_by, "reviewed_at": i.reviewed_at, "dispatched_at": i.dispatched_at, "arrived_at": i.arrived_at, "loaded_at": i.loaded_at,
        "hospital_arrived_at": i.hospital_arrived_at, "completed_at": i.completed_at, "cancelled_at": i.cancelled_at,
        "source": i.source, "created_by": i.created_by,
        "response_time_s": sim_seconds(i.created_at, i.arrived_at) if i.source != "HISTORICAL_SEED" else i.historical_response_s,
    }


def set_status(db: Session, inc: EmergencyIncident, status: str, **extra) -> None:
    old = inc.status
    inc.status = status
    emit(db, "EMERGENCY_STATUS_CHANGED", {"incident_id": str(inc.id), "reference": inc.reference, "old_status": old,
                                          "status": status, **extra}, incident_id=inc.id,
         ambulance_id=inc.assigned_ambulance)
    msg = {"incident_id": str(inc.id), "status": status, "ts": utcnow().isoformat()}
    after_commit(db, lambda: publish(f"emergency/{inc.id}/status", msg))


def compute_priority(db: Session, inc: EmergencyIncident) -> tuple[float, dict]:
    row = db.execute(text(
        "SELECT ST_Distance(a.location, i.location) FROM ambulances a, emergency_incidents i "
        "WHERE i.id = :id AND a.status = 'AVAILABLE' ORDER BY a.location <-> i.location LIMIT 1"),
        {"id": inc.id}).first()
    nearest = float(row[0]) if row else None
    capable = CAPABLE[inc.required_capability or "BASIC"]
    total = db.scalar(select(func.count()).select_from(Ambulance).where(
        Ambulance.equipment_level.in_(capable), Ambulance.status.notin_(("MAINTENANCE", "OFFLINE")))) or 0
    avail = db.scalar(select(func.count()).select_from(Ambulance).where(
        Ambulance.equipment_level.in_(capable), Ambulance.status == "AVAILABLE")) or 0
    waiting = sim_seconds(inc.created_at, utcnow()) or 0.0
    score, comps = priority_score(inc.severity, inc.rule_score or 0, waiting, nearest, avail, total)
    comps.update({"waiting_s": round(waiting, 1), "nearest_available_m": None if nearest is None else round(nearest, 1),
                  "available_capable": avail, "total_capable": total})
    return score, comps


def next_reference(db: Session) -> str:
    seq = db.scalar(text("SELECT nextval('incident_reference_seq')"))
    return f"INC-{seq:05d}"


def create_incident(db: Session, data: dict, created_by: str | None = None, source: str = "LIVE") -> EmergencyIncident:
    lat, lon = data["latitude"], data["longitude"]
    inside = inside_service_area(db, lat, lon)
    if inside is False:
        raise IncidentValidationError(f"location ({lat:.5f}, {lon:.5f}) is outside the service area")
    road_d = distance_to_road_m(db, lat, lon)
    if road_d is not None and road_d > MAX_ROAD_DISTANCE_M:
        raise IncidentValidationError(
            f"location ({lat:.5f}, {lon:.5f}) is {road_d:.0f} m from the nearest road - an ambulance cannot reach it. "
            f"Place the incident on or near a road (within {MAX_ROAD_DISTANCE_M:.0f} m).")
    inc = EmergencyIncident(
        id=uuid.uuid4(), reference=next_reference(db), latitude=lat, longitude=lon, location=point_wkt(lat, lon),
        address=data.get("address"), emergency_type=data["emergency_type"], patient_age=data["patient_age"],
        heart_rate=data["heart_rate"], respiratory_rate=data["respiratory_rate"],
        oxygen_saturation=data.get("oxygen_saturation"), systolic_bp=data.get("systolic_bp"),
        temperature_c=data.get("temperature_c"), consciousness=data["consciousness"],
        bleeding=data["bleeding"], injury_severity=data["injury_severity"], accident_type=data["accident_type"],
        breathing_difficulty=data["breathing_difficulty"], chest_pain=data.get("chest_pain", False),
        notes=data.get("notes"), status="CREATED", created_by=created_by, source=source, created_at=utcnow(),
    )
    db.add(inc)
    db.flush()
    emit(db, "EMERGENCY_CREATED", {"incident_id": str(inc.id), "reference": inc.reference, "latitude": lat,
                                   "longitude": lon, "emergency_type": inc.emergency_type, "source": source},
         incident_id=inc.id)
    log_event(log, "EMERGENCY_CREATED", incident_id=str(inc.id), reference=inc.reference, type=inc.emergency_type)
    set_status(db, inc, "CLASSIFYING")
    classify(db, inc, data)
    priority, comps = compute_priority(db, inc)
    inc.priority, inc.priority_components = priority, comps
    set_status(db, inc, "WAITING", priority=priority)
    metrics.EMERGENCIES.labels(inc.severity).inc()
    created_msg = {"incident_id": str(inc.id), "reference": inc.reference, "latitude": lat, "longitude": lon,
                   "emergency_type": inc.emergency_type, "severity": inc.severity, "priority": priority}
    after_commit(db, lambda: publish(f"emergency/{inc.id}/created", created_msg))
    return inc


def classify(db: Session, inc: EmergencyIncident, case: dict) -> None:
    rule = severity_score(case)
    inc.rule_score, inc.rule_severity, inc.rule_components = rule.score, rule.level, rule.components
    ml_level = None
    probabilities = None
    try:
        pred = STATE.model.predict(case)
        ml_level = pred.severity
        probabilities = pred.probabilities
        inc.predicted_severity, inc.ml_confidence, inc.ml_status = pred.severity, pred.confidence, "OK"
        db.add(ModelPrediction(incident_id=inc.id, model_name=pred.model_name, model_version=pred.model_version,
                               features=pred.features, predicted_class=pred.severity, probabilities=pred.probabilities,
                               latency_ms=pred.latency_ms))
        log_event(log, "ML_PREDICTION", incident_id=str(inc.id), predicted=pred.severity,
                  confidence=round(pred.confidence, 3), model_version=pred.model_version)
    except ModelUnavailable as exc:
        inc.ml_status = "UNAVAILABLE"
        log_event(log, "ML_PREDICTION_UNAVAILABLE", incident_id=str(inc.id), error=str(exc))
    final, basis = combine_severity(ml_level, rule.level)
    inc.severity = final
    inc.severity_reasons = rule.reasons + [f"final severity basis: {basis}"]
    inc.required_capability = required_capability(final, inc.emergency_type)
    st = get_settings()
    ca = assess(probabilities, ml_level, st.dispatch_confidence_high, st.dispatch_confidence_low,
                safety_override=basis.startswith("SAFETY_OVERRIDE"))
    inc.class_probabilities = probabilities
    inc.confidence_level, inc.decision_mode, inc.decision_reason = ca.confidence_level, ca.decision_mode, ca.reason
    emit(db, "EMERGENCY_CLASSIFIED", {"incident_id": str(inc.id), "predicted_severity": ml_level,
                                      "ml_confidence": inc.ml_confidence, "ml_status": inc.ml_status,
                                      "rule_score": rule.score, "rule_severity": rule.level, "severity": final,
                                      "basis": basis, "reasons": rule.reasons}, incident_id=inc.id)
    emit(db, "CONFIDENCE_ASSESSED", {"incident_id": str(inc.id), "predicted_severity": ml_level,
                                     "model_version": STATE.model.version, **ca.as_dict(),
                                     "thresholds": {"high": st.dispatch_confidence_high, "low": st.dispatch_confidence_low}},
         incident_id=inc.id)
    log_event(log, "CONFIDENCE_ASSESSED", incident_id=str(inc.id), predicted=ml_level, confidence=ca.confidence,
              level=ca.confidence_level, decision=ca.decision_mode, model_version=STATE.model.version)


def review_incident(db: Session, inc: EmergencyIncident, user: str, severity: str | None = None) -> None:
    """Dispatcher confirms (or overrides) the severity of an incident; this authorises dispatch."""
    from app.dispatch.priority import LEVEL_VALUE
    old = inc.severity
    if severity and severity != inc.severity:
        if severity not in LEVEL_VALUE:
            raise IncidentValidationError(f"unknown severity {severity}")
        inc.severity = severity
        inc.required_capability = required_capability(severity, inc.emergency_type)
        inc.severity_reasons = (inc.severity_reasons or []) + [f"severity set to {severity} by dispatcher {user}"]
    inc.reviewed_by, inc.reviewed_at = user, utcnow()
    prev_mode = inc.decision_mode
    inc.decision_mode = "HUMAN_APPROVED"
    inc.decision_reason = (f"Reviewed by {user}" + (f": severity {old} -> {inc.severity}" if old != inc.severity else
                           f": severity {inc.severity} confirmed") + f" (was {prev_mode}).")
    score, comps = compute_priority(db, inc)
    inc.priority, inc.priority_components = score, comps
    emit(db, "HUMAN_REVIEW_COMPLETED", {"incident_id": str(inc.id), "reference": inc.reference, "reviewed_by": user,
                                        "old_severity": old, "severity": inc.severity, "previous_mode": prev_mode},
         incident_id=inc.id)
    log_event(log, "HUMAN_REVIEW_COMPLETED", incident_id=str(inc.id), severity=inc.severity, previous_mode=prev_mode)


def refresh_waiting_priorities(db: Session) -> list[EmergencyIncident]:
    """Recompute PriorityScore of every WAITING incident (waiting time & fleet availability change)."""
    waiting = db.scalars(select(EmergencyIncident).where(EmergencyIncident.status == "WAITING")
                         .with_for_update(skip_locked=True)).all()
    for inc in waiting:
        score, comps = compute_priority(db, inc)
        if inc.priority is None or abs(score - inc.priority) >= 1.0:
            emit(db, "EMERGENCY_PRIORITY_CHANGED", {"incident_id": str(inc.id), "reference": inc.reference,
                                                    "old_priority": inc.priority, "priority": score,
                                                    "components": comps}, incident_id=inc.id, persist=False)
        inc.priority, inc.priority_components = score, comps
    return list(waiting)
