import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import any_user, dispatcher
from app.config import get_settings
from app.database import get_db
from app.models import Dispatch, EmergencyIncident, ModelPrediction, Route, SystemEvent, User
from app.schemas.schemas import DispatchRequest, EmergencyCreate
from app.services.dispatch_service import NoAmbulanceAvailable, dispatch_incident, dispatcher_cycle, preview
from app.services.incident_service import IncidentValidationError, create_incident, incident_dict
from app.services.mission_service import cancel_incident
from app.services.state import STATE
from app.routing.engine import NoRouteError
from app.api.routes import route_dict

router = APIRouter(prefix="/api/emergencies", tags=["emergencies"])


def _get(db: Session, incident_id: str) -> EmergencyIncident:
    try:
        uid = uuid.UUID(incident_id)
    except ValueError:
        inc = db.scalar(select(EmergencyIncident).where(EmergencyIncident.reference == incident_id))
    else:
        inc = db.get(EmergencyIncident, uid)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


@router.post("", status_code=201)
def create(body: EmergencyCreate, user: User = Depends(dispatcher), db: Session = Depends(get_db)):
    """Steps 1-5: validate, ML severity, rule score, priority; then (optionally) auto-dispatch."""
    try:
        with STATE.lock:
            inc = create_incident(db, body.model_dump(), created_by=user.username)
            db.commit()
    except IncidentValidationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    auto = get_settings().auto_dispatch if body.auto_dispatch is None else body.auto_dispatch
    dispatch_error = None
    if auto:
        try:
            dispatcher_cycle()
        except Exception as exc:  # dispatch failure must not lose the created incident
            dispatch_error = str(exc)
    db.expire_all()
    return detail(str(inc.id), db, dispatch_error=dispatch_error)


@router.get("")
def list_incidents(status: str | None = None, active: bool = False, source: str | None = None,
                   limit: int = Query(200, le=2000), _: User = Depends(any_user), db: Session = Depends(get_db)):
    q = select(EmergencyIncident)
    if status:
        q = q.where(EmergencyIncident.status.in_(status.split(",")))
    if active:
        q = q.where(EmergencyIncident.status.notin_(("COMPLETED", "CANCELLED")))
    if source:
        q = q.where(EmergencyIncident.source == source)
    q = q.order_by(EmergencyIncident.created_at.desc()).limit(limit)
    return [incident_dict(i) for i in db.scalars(q)]


@router.get("/{incident_id}")
def get_incident(incident_id: str, _: User = Depends(any_user), db: Session = Depends(get_db)):
    return detail(incident_id, db)


def detail(incident_id: str, db: Session, dispatch_error: str | None = None) -> dict:
    inc = _get(db, incident_id)
    d = incident_dict(inc)
    disp = db.scalars(select(Dispatch).where(Dispatch.incident_id == inc.id).order_by(Dispatch.created_at.desc())).first()
    d["dispatch"] = None if disp is None else {
        "id": str(disp.id), "ambulance_id": disp.ambulance_id, "method": disp.method, "score": disp.dispatch_score,
        "eta_to_patient_s": disp.eta_to_patient_s, "distance_to_patient_m": disp.distance_to_patient_m,
        "candidates": disp.candidates, "explanation": disp.explanation, "decision_ms": disp.decision_ms,
        "hospital_id": disp.hospital_id, "hospital_candidates": disp.hospital_candidates,
        "hospital_explanation": disp.hospital_explanation, "status": disp.status, "created_at": disp.created_at}
    d["routes"] = [route_dict(r, db) for r in db.scalars(
        select(Route).where(Route.incident_id == inc.id).order_by(Route.created_at))]
    pred = db.scalars(select(ModelPrediction).where(ModelPrediction.incident_id == inc.id)).first()
    d["ml_prediction"] = None if pred is None else {
        "model_name": pred.model_name, "model_version": pred.model_version, "predicted_class": pred.predicted_class,
        "probabilities": pred.probabilities, "latency_ms": pred.latency_ms, "features": pred.features}
    d["timeline"] = [{"type": e.event_type, "at": e.created_at, "data": e.payload} for e in db.scalars(
        select(SystemEvent).where(SystemEvent.incident_id == inc.id).order_by(SystemEvent.created_at, SystemEvent.id))]
    from app.services.routes_service import ACTIVE, remaining_eta
    ar = ACTIVE.get(inc.assigned_ambulance) if inc.assigned_ambulance else None
    d["live_eta_s"] = round(remaining_eta(ar), 1) if ar and ar.incident_id == inc.id else None
    if dispatch_error:
        d["dispatch_error"] = dispatch_error
    return d


@router.post("/{incident_id}/dispatch")
def dispatch(incident_id: str, body: DispatchRequest | None = None, user: User = Depends(dispatcher),
             db: Session = Depends(get_db)):
    """Steps 6-11: candidates → routes → traffic-aware ETA → DispatchScore → best ambulance."""
    with STATE.lock:
        inc = _get(db, incident_id)
        try:
            dispatch_incident(db, inc, body.ambulance_id if body else None, by=user.username)
            db.commit()
        except NoAmbulanceAvailable as exc:
            db.rollback()
            raise HTTPException(409, str(exc))
        except (ValueError, NoRouteError) as exc:
            db.rollback()
            raise HTTPException(409, str(exc))
    db.expire_all()
    return detail(incident_id, db)


@router.get("/{incident_id}/candidates")
def candidates(incident_id: str, _: User = Depends(any_user), db: Session = Depends(get_db)):
    inc = _get(db, incident_id)
    try:
        return preview(db, inc)
    except NoAmbulanceAvailable as exc:
        raise HTTPException(409, str(exc))


@router.post("/{incident_id}/cancel")
def cancel(incident_id: str, user: User = Depends(dispatcher), db: Session = Depends(get_db)):
    with STATE.lock:
        inc = _get(db, incident_id)
        try:
            cancel_incident(db, inc, by=user.username)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
    return detail(incident_id, db)


@router.post("/{incident_id}/reroute")
def reroute(incident_id: str, _: User = Depends(dispatcher), db: Session = Depends(get_db)):
    """Dispatcher-requested route re-evaluation for the incident's ambulance."""
    from app.services.routes_service import check_routes
    inc = _get(db, incident_id)
    if not inc.assigned_ambulance:
        raise HTTPException(409, "incident has no assigned ambulance")
    res = check_routes(force_ambulance=inc.assigned_ambulance)
    return {"results": res}
