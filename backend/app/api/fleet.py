"""Ambulances and hospitals."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import admin, any_user
from app.database import get_db
from app.models import Ambulance, Hospital, User
from app.models.entities import point_wkt
from app.schemas.schemas import AmbulanceCreate, AmbulanceUpdate, HospitalCreate, HospitalUpdate
from app.services.events import emit
from app.services.mission_service import hospital_dict, set_amb_status
from app.services.state import STATE
from app.services.telemetry_service import TELEMETRY

router = APIRouter(prefix="/api", tags=["fleet"])


def ambulance_dict(a: Ambulance) -> dict:
    live = TELEMETRY.latest.get(a.id, {})
    lat = live.get("latitude", a.latitude) if a.status != "AVAILABLE" else a.latitude
    lon = live.get("longitude", a.longitude) if a.status != "AVAILABLE" else a.longitude
    return {"id": a.id, "call_sign": a.call_sign, "latitude": lat, "longitude": lon, "status": a.status,
            "capacity": a.capacity, "equipment_level": a.equipment_level, "driver_status": a.driver_status,
            "current_incident": str(a.current_incident) if a.current_incident else None,
            "fuel_level": round(a.fuel_level, 1), "current_speed": round(live.get("speed", a.current_speed), 1),
            "destination": a.destination, "destination_lat": a.destination_lat, "destination_lon": a.destination_lon,
            "missions_today": a.missions_today, "last_updated": live.get("timestamp", a.last_updated),
            "eta_remaining_s": live.get("eta_remaining_s") if a.status not in ("AVAILABLE", "OFFLINE", "MAINTENANCE") else None,
            "base_latitude": a.base_latitude, "base_longitude": a.base_longitude}


@router.get("/ambulances", tags=["fleet"])
def list_ambulances(status: str | None = None, near_lat: float | None = None, near_lon: float | None = None,
                    radius_m: float = 5000, _: User = Depends(any_user), db: Session = Depends(get_db)):
    q = select(Ambulance)
    if status:
        q = q.where(Ambulance.status.in_(status.split(",")))
    if near_lat is not None and near_lon is not None:
        q = q.where(text("ST_DWithin(ambulances.location, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography, :r)")
                    .bindparams(lat=near_lat, lon=near_lon, r=radius_m))
    return [ambulance_dict(a) for a in db.scalars(q.order_by(Ambulance.id))]


@router.get("/ambulances/{ambulance_id}")
def get_ambulance(ambulance_id: str, _: User = Depends(any_user), db: Session = Depends(get_db)):
    a = db.get(Ambulance, ambulance_id)
    if a is None:
        raise HTTPException(404, "ambulance not found")
    d = ambulance_dict(a)
    d["recent_track"] = [dict(r._mapping) for r in db.execute(text(
        "SELECT latitude, longitude, speed_kph, recorded_at FROM ambulance_locations WHERE ambulance_id=:id "
        "ORDER BY recorded_at DESC LIMIT 300"), {"id": ambulance_id})]
    return d


@router.post("/ambulances", status_code=201)
def create_ambulance(body: AmbulanceCreate, _: User = Depends(admin), db: Session = Depends(get_db)):
    if db.get(Ambulance, body.id):
        raise HTTPException(409, "ambulance id exists")
    a = Ambulance(id=body.id, call_sign=body.call_sign, latitude=body.latitude, longitude=body.longitude,
                  location=point_wkt(body.latitude, body.longitude), base_latitude=body.latitude,
                  base_longitude=body.longitude, equipment_level=body.equipment_level, capacity=body.capacity,
                  fuel_level=body.fuel_level, status="AVAILABLE", driver_status="ON_DUTY", missions_today=0)
    db.add(a)
    emit(db, "AMBULANCE_STATUS_CHANGED", {"ambulance_id": a.id, "old_status": None, "status": "AVAILABLE"}, ambulance_id=a.id)
    db.commit()
    return ambulance_dict(a)


@router.patch("/ambulances/{ambulance_id}")
def update_ambulance(ambulance_id: str, body: AmbulanceUpdate, _: User = Depends(admin), db: Session = Depends(get_db)):
    with STATE.lock:
        a = db.get(Ambulance, ambulance_id, with_for_update=True)
        if a is None:
            raise HTTPException(404, "ambulance not found")
        if body.status and a.current_incident is not None:
            raise HTTPException(409, "ambulance is on a mission; status cannot be changed manually")
        if body.status:
            set_amb_status(db, a, body.status)
        for f in ("equipment_level", "driver_status", "fuel_level"):
            v = getattr(body, f)
            if v is not None:
                setattr(a, f, v)
        db.commit()
    return ambulance_dict(a)


@router.get("/hospitals")
def list_hospitals(_: User = Depends(any_user), db: Session = Depends(get_db)):
    return [hospital_dict(h) for h in db.scalars(select(Hospital).order_by(Hospital.id))]


@router.post("/hospitals", status_code=201)
def create_hospital(body: HospitalCreate, _: User = Depends(admin), db: Session = Depends(get_db)):
    if db.get(Hospital, body.id):
        raise HTTPException(409, "hospital id exists")
    h = Hospital(**body.model_dump(), location=point_wkt(body.latitude, body.longitude), current_load=0, status="ACTIVE")
    db.add(h)
    db.commit()
    return hospital_dict(h)


@router.patch("/hospitals/{hospital_id}")
def update_hospital(hospital_id: str, body: HospitalUpdate, _: User = Depends(admin), db: Session = Depends(get_db)):
    h = db.get(Hospital, hospital_id, with_for_update=True)
    if h is None:
        raise HTTPException(404, "hospital not found")
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(h, f, v)
    emit(db, "HOSPITAL_CAPACITY_CHANGED", hospital_dict(h))
    db.commit()
    return hospital_dict(h)
