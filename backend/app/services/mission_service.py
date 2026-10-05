"""Mission state machine driven by simulator events (MQTT) and simulated time.

DISPATCHED --ROUTE_STARTED--> EN_ROUTE --ARRIVED--> ARRIVED (at scene)
  --scene time--> PATIENT_LOADED --hospital selection--> TO_HOSPITAL --ARRIVED--> (at hospital)
  --handover time--> COMPLETED ; ambulance AVAILABLE again.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.scoring import HospitalInput, explain_hospital, hospital_requirements, score_hospitals
from app.models import Ambulance, Dispatch, EmergencyIncident, Hospital
from app.mqtt.client import publish
from app.routing.engine import NoRouteError
from app.services import metrics
from app.services.events import after_commit, emit
from app.services.incident_service import set_status
from app.services.routes_service import ACTIVE, activate, complete_route, save_route
from app.services.state import STATE, require_router
from app.utils.logging import log_event
from app.utils.timeutil import sim_seconds, utcnow

log = logging.getLogger("app.missions")


def set_amb_status(db: Session, amb: Ambulance, status: str, incident_id=None) -> None:
    old = amb.status
    if old == status:
        return
    amb.status = status
    amb.last_updated = utcnow()
    emit(db, "AMBULANCE_STATUS_CHANGED", {"ambulance_id": amb.id, "old_status": old, "status": status,
                                          "incident_id": str(incident_id) if incident_id else None},
         incident_id=incident_id, ambulance_id=amb.id)
    msg = {"ambulance_id": amb.id, "status": status, "source": "BACKEND", "ts": utcnow().isoformat()}
    after_commit(db, lambda: publish(f"ambulance/{amb.id}/status", msg))


def handle_sim_status(db: Session, ambulance_id: str, payload: dict) -> None:
    """Events reported by the ambulance simulator."""
    event = payload.get("event")
    amb = db.get(Ambulance, ambulance_id, with_for_update=True)
    if amb is None or not event:
        return
    ar = ACTIVE.get(ambulance_id)
    route_id = payload.get("route_id")
    if ar is None or (route_id and str(ar.route_id) != route_id):
        return  # stale event for a superseded route
    inc = db.get(EmergencyIncident, ar.incident_id, with_for_update=True) if ar.incident_id else None
    if inc is None:
        return
    if event == "ROUTE_STARTED":
        if ar.leg == "TO_PATIENT" and amb.status == "DISPATCHED":
            set_amb_status(db, amb, "EN_ROUTE_TO_PATIENT", inc.id)
            if inc.status == "DISPATCHED":
                set_status(db, inc, "EN_ROUTE", ambulance_id=amb.id)
    elif event == "ARRIVED":
        complete_route(db, ambulance_id)
        if ar.leg == "TO_PATIENT":
            inc.arrived_at = utcnow()
            if inc.status in ("DISPATCHED", "EN_ROUTE"):
                set_status(db, inc, "ARRIVED", ambulance_id=amb.id,
                           response_time_s=sim_seconds(inc.created_at, inc.arrived_at))
            set_amb_status(db, amb, "AT_SCENE", inc.id)
            rt = sim_seconds(inc.created_at, inc.arrived_at)
            if rt is not None:
                metrics.RESPONSE_TIME.observe(rt)
            log_event(log, "AMBULANCE_ARRIVED_SCENE", incident_id=str(inc.id), ambulance_id=amb.id, response_time_s=rt)
        elif ar.leg == "TO_HOSPITAL":
            inc.hospital_arrived_at = utcnow()
            set_amb_status(db, amb, "AT_HOSPITAL", inc.id)
            emit(db, "AMBULANCE_AT_HOSPITAL", {"incident_id": str(inc.id), "ambulance_id": amb.id,
                                               "hospital_id": inc.destination_hospital}, incident_id=inc.id,
                 ambulance_id=amb.id)


def select_hospital(db: Session, inc: EmergencyIncident, origin: tuple[float, float]):
    router = require_router()
    rows = db.execute(text(
        "SELECT h.id FROM hospitals h WHERE h.status <> 'CLOSED' "
        "ORDER BY h.location <-> ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography LIMIT 8"),
        {"lat": origin[0], "lon": origin[1]}).all()
    hospitals = {h.id: h for h in db.scalars(select(Hospital).where(Hospital.id.in_([r[0] for r in rows])))}
    inputs, routes = [], {}
    for hid, h in hospitals.items():
        try:
            rr = router.route(origin, (h.latitude, h.longitude), compute_shortest=False)
        except NoRouteError:
            continue
        routes[hid] = rr
        inputs.append(HospitalInput(hid, h.name, rr.adjusted_duration_s, rr.traffic_delay_s, rr.distance_m, h.current_load,
                                    h.emergency_capacity, h.icu_available, h.trauma_available, h.cardiac_available,
                                    h.stroke_available))
    reqs = hospital_requirements(inc.severity or "MEDIUM", inc.emergency_type)
    ranked = score_hospitals(inputs, reqs)
    return ranked, routes, reqs, hospitals


def load_patient_and_transport(db: Session, inc: EmergencyIncident, amb: Ambulance) -> None:
    inc.loaded_at = utcnow()
    set_status(db, inc, "PATIENT_LOADED", ambulance_id=amb.id)
    origin = (amb.latitude, amb.longitude)
    ranked, routes, reqs, hospitals = select_hospital(db, inc, origin)
    if not ranked:
        emit(db, "HOSPITAL_WARNING", {"incident_id": str(inc.id), "reference": inc.reference,
                                      "warning": "No reachable hospital - dispatcher action required"},
             incident_id=inc.id, ambulance_id=amb.id)
        return
    best = ranked[0]
    explanation = explain_hospital(ranked, reqs)
    if best["missing_capabilities"] or best["full"]:
        emit(db, "HOSPITAL_WARNING", {"incident_id": str(inc.id), "reference": inc.reference,
                                      "warning": f"No fully suitable hospital: {best['name']} lacks "
                                                 f"{', '.join(best['missing_capabilities']) or 'capacity'}"},
             incident_id=inc.id, ambulance_id=amb.id)
    hosp = db.get(Hospital, best["hospital_id"], with_for_update=True)
    hosp.current_load += 1
    if inc.severity == "CRITICAL" and hosp.icu_available > 0:
        hosp.icu_available -= 1
    hosp.updated_at = utcnow()
    inc.destination_hospital = hosp.id
    disp = db.scalar(select(Dispatch).where(Dispatch.incident_id == inc.id, Dispatch.status == "ACTIVE"))
    if disp:
        disp.hospital_id, disp.hospital_candidates, disp.hospital_explanation = hosp.id, ranked, explanation
    rr = routes[hosp.id]
    dest = (hosp.latitude, hosp.longitude)
    rr.shortest_distance_m = require_router().shortest_distance(origin, dest)
    route = save_route(db, rr, leg="TO_HOSPITAL", origin=origin, dest=dest, ambulance_id=amb.id, incident_id=inc.id,
                       dispatch_id=disp.id if disp else None)
    emit(db, "HOSPITAL_SELECTED", {"incident_id": str(inc.id), "reference": inc.reference, "hospital_id": hosp.id,
                                   "hospital_name": hosp.name, "score": best["score"], "explanation": explanation,
                                   "candidates": ranked, "requirements": reqs, "route_id": str(route.id),
                                   "eta_s": round(rr.adjusted_duration_s, 1),
                                   "geometry": [[round(a, 6), round(b, 6)] for a, b in rr.coords]},
         incident_id=inc.id, ambulance_id=amb.id)
    emit(db, "HOSPITAL_CAPACITY_CHANGED", hospital_dict(hosp))
    cap_msg = {**hospital_dict(hosp), "source": "BACKEND"}
    after_commit(db, lambda: publish(f"hospital/{hosp.id}/capacity", cap_msg, retain=True))
    log_event(log, "HOSPITAL_SELECTED", incident_id=str(inc.id), hospital_id=hosp.id, score=best["score"],
              eta_s=round(rr.adjusted_duration_s, 1))
    amb.destination, amb.destination_lat, amb.destination_lon = hosp.name[:64], dest[0], dest[1]
    set_status(db, inc, "TO_HOSPITAL", ambulance_id=amb.id, hospital_id=hosp.id)
    set_amb_status(db, amb, "TRANSPORTING", inc.id)
    activate(db, route, rr, amb.id, inc.id, "TO_HOSPITAL", dest)


def complete_incident(db: Session, inc: EmergencyIncident, amb: Ambulance) -> None:
    inc.completed_at = utcnow()
    set_status(db, inc, "COMPLETED", ambulance_id=amb.id)
    disp = db.scalar(select(Dispatch).where(Dispatch.incident_id == inc.id, Dispatch.status == "ACTIVE"))
    if disp:
        disp.status = "COMPLETED"
    amb.current_incident = None
    amb.destination = amb.destination_lat = amb.destination_lon = None
    amb.missions_today += 1
    amb.current_speed = 0
    set_amb_status(db, amb, "AVAILABLE")
    complete_route(db, amb.id)
    after_commit(db, lambda: publish(f"ambulance/{amb.id}/command", {"command": "IDLE", "ts": utcnow().isoformat()},
                                     retain=True))
    metrics.COMPLETIONS.inc()
    emit(db, "INCIDENT_COMPLETED", {"incident_id": str(inc.id), "reference": inc.reference, "ambulance_id": amb.id,
                                    "hospital_id": inc.destination_hospital,
                                    "response_time_s": sim_seconds(inc.created_at, inc.arrived_at),
                                    "total_time_s": sim_seconds(inc.created_at, inc.completed_at)},
         incident_id=inc.id, ambulance_id=amb.id)
    log_event(log, "INCIDENT_COMPLETED", incident_id=str(inc.id), ambulance_id=amb.id,
              response_time_s=sim_seconds(inc.created_at, inc.arrived_at))


def mission_tick() -> int:
    """Advance time-based transitions (scene time, hospital handover) using simulated time."""
    from app.database import session_scope

    s = get_settings()
    now = utcnow()
    scene_wall = timedelta(seconds=s.scene_time_s / s.sim_time_scale)
    handover_wall = timedelta(seconds=s.handover_time_s / s.sim_time_scale)
    n = 0
    with STATE.lock, session_scope() as db:
        ready = db.scalars(select(EmergencyIncident).where(
            EmergencyIncident.status == "ARRIVED", EmergencyIncident.arrived_at <= now - scene_wall)
            .with_for_update(skip_locked=True)).all()
        for inc in ready:
            n += _transition(db, inc, load_patient_and_transport)
        done = db.scalars(select(EmergencyIncident).where(
            EmergencyIncident.status == "TO_HOSPITAL", EmergencyIncident.hospital_arrived_at.is_not(None),
            EmergencyIncident.hospital_arrived_at <= now - handover_wall).with_for_update(skip_locked=True)).all()
        for inc in done:
            n += _transition(db, inc, complete_incident)
    return n


def _transition(db: Session, inc: EmergencyIncident, fn) -> int:
    """Run one incident transition inside a savepoint so a failure cannot block the other incidents."""
    amb = db.get(Ambulance, inc.assigned_ambulance, with_for_update=True) if inc.assigned_ambulance else None
    if amb is None:
        return 0
    try:
        with db.begin_nested():
            fn(db, inc, amb)
        return 1
    except Exception:
        log.exception("mission transition failed", extra={"event": "MISSION_TRANSITION_FAILED",
                                                          "fields": {"incident_id": str(inc.id), "step": fn.__name__}})
        return 0


def cancel_incident(db: Session, inc: EmergencyIncident, by: str | None = None) -> None:
    if inc.status in ("COMPLETED", "CANCELLED"):
        raise ValueError(f"incident already {inc.status}")
    inc.cancelled_at = utcnow()
    if inc.assigned_ambulance and inc.status not in ("COMPLETED",):
        amb = db.get(Ambulance, inc.assigned_ambulance, with_for_update=True)
        if amb and amb.current_incident == inc.id:
            amb.current_incident = None
            amb.destination = amb.destination_lat = amb.destination_lon = None
            set_amb_status(db, amb, "AVAILABLE")
            complete_route(db, amb.id)
            after_commit(db, lambda: publish(f"ambulance/{amb.id}/command", {"command": "IDLE"}, retain=True))
        disp = db.scalar(select(Dispatch).where(Dispatch.incident_id == inc.id, Dispatch.status == "ACTIVE"))
        if disp:
            disp.status = "CANCELLED"
    set_status(db, inc, "CANCELLED", by=by)


def hospital_dict(h: Hospital) -> dict:
    return {"id": h.id, "name": h.name, "latitude": h.latitude, "longitude": h.longitude,
            "emergency_capacity": h.emergency_capacity, "icu_available": h.icu_available,
            "trauma_available": h.trauma_available, "cardiac_available": h.cardiac_available,
            "stroke_available": h.stroke_available, "current_load": h.current_load, "status": h.status,
            "load_pct": round(100 * h.current_load / h.emergency_capacity, 1), "updated_at": h.updated_at}


def hospital_discharge(db: Session, hospital_id: str, count: int = 1) -> None:
    h = db.get(Hospital, hospital_id, with_for_update=True)
    if h is None or h.current_load <= 0:
        return
    h.current_load = max(0, h.current_load - count)
    h.updated_at = utcnow()
    emit(db, "HOSPITAL_CAPACITY_CHANGED", hospital_dict(h))
    cap_msg = {**hospital_dict(h), "source": "BACKEND"}
    after_commit(db, lambda: publish(f"hospital/{h.id}/capacity", cap_msg, retain=True))


