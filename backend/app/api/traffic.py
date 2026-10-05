import random

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import any_user, dispatcher
from app.database import get_db
from app.models import RoadCondition, TrafficEvent, User
from app.schemas.schemas import TrafficEventCreate, TrafficSimulateRequest
from app.services.state import STATE
from app.services.traffic_service import TrafficError, create_event, nearest_road, reset_all, road_dict, simulate_step

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


@router.get("/roads")
def roads(congested_only: bool = False, highway: str | None = None, _: User = Depends(any_user),
          db: Session = Depends(get_db)):
    """Road segments with live traffic state and geometry ([lat, lon] lists)."""
    sql = ("SELECT road_id, ST_AsGeoJSON(geom::geometry, 6) AS gj FROM road_conditions WHERE true "
           + ("AND (congestion_level <> 'FREE' OR blocked) " if congested_only else "")
           + ("AND highway_type = ANY(:hw) " if highway else ""))
    import json
    geo = {r[0]: [[c[1], c[0]] for c in json.loads(r[1])["coordinates"]]
           for r in db.execute(text(sql), {"hw": highway.split(",") if highway else None})}
    rows = db.scalars(select(RoadCondition).where(RoadCondition.road_id.in_(list(geo)))).all() if geo else []
    return [dict(road_dict(r), geometry=geo[r.road_id]) for r in rows]


@router.get("/roads/nearest")
def road_nearest(lat: float, lon: float, _: User = Depends(any_user), db: Session = Depends(get_db)):
    r = nearest_road(db, lat, lon)
    if r is None:
        raise HTTPException(404, "no roads loaded")
    return r


@router.get("/events")
def events(limit: int = 100, active_only: bool = False, _: User = Depends(any_user), db: Session = Depends(get_db)):
    q = select(TrafficEvent)
    if active_only:
        q = q.where(TrafficEvent.active.is_(True), TrafficEvent.event_type.in_(("ACCIDENT", "BLOCK")))
    rows = db.scalars(q.order_by(TrafficEvent.created_at.desc()).limit(limit)).all()
    return [{"id": str(e.id), "created_at": e.created_at, "road_id": e.road_id, "event_type": e.event_type,
             "old_level": e.old_level, "new_level": e.new_level, "blocked": e.blocked,
             "incident_multiplier": e.incident_multiplier, "source": e.source, "active": e.active} for e in rows]


@router.post("/events", status_code=201)
def create(body: TrafficEventCreate, _: User = Depends(dispatcher), db: Session = Depends(get_db)):
    """Dispatcher traffic control: ACCIDENT / BLOCK / UNBLOCK / CONGESTION(level) / CLEAR."""
    try:
        changes = create_event(db, body.event_type, road_id=body.road_id, lat=body.latitude, lon=body.longitude,
                               level=body.level, radius_m=body.radius_m, source="DISPATCHER")
        db.commit()
    except TrafficError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return {"changes": changes}


@router.post("/simulate")
def simulate(body: TrafficSimulateRequest, _: User = Depends(dispatcher), db: Session = Depends(get_db)):
    rng = random.Random(body.seed)
    changes = []
    for _ in range(body.steps):
        changes += simulate_step(db, rng, body.changes_per_step, source="SIMULATION")
        db.commit()
    return {"changes": changes, "count": len(changes)}


@router.post("/reset")
def reset(_: User = Depends(dispatcher), db: Session = Depends(get_db)):
    n = reset_all(db)
    db.commit()
    return {"cleared_roads": n}


@router.get("/network")
def network(_: User = Depends(any_user)):
    if STATE.graph is None:
        raise HTTPException(503, STATE.graph_error or "road network not loaded")
    return STATE.graph.stats()
