from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import any_user
from app.database import get_db
from app.models import Route, RouteSegment, User
from app.routing.engine import NoRouteError
from app.routing.eta import route_efficiency
from app.schemas.schemas import RouteRequest
from app.services.routes_service import ACTIVE, remaining_eta, save_route
from app.services.state import require_router

router = APIRouter(prefix="/api/routes", tags=["routing"])


def _shape(geom):
    if isinstance(geom, str):                      # freshly created row: still the WKT we assigned
        from shapely import wkt
        return wkt.loads(geom.split(";", 1)[-1])
    return to_shape(geom)


def route_dict(r: Route, db: Session | None = None, with_segments: bool = False) -> dict:
    coords = [[lat, lon] for lon, lat in _shape(r.geometry).coords]
    d = {"id": str(r.id), "incident_id": str(r.incident_id) if r.incident_id else None, "ambulance_id": r.ambulance_id,
         "leg": r.leg, "engine": r.engine, "network_source": r.network_source, "distance_m": r.distance_m,
         "base_duration_s": r.base_duration_s, "adjusted_duration_s": r.adjusted_duration_s,
         "osrm_duration_s": r.osrm_duration_s, "shortest_distance_m": r.shortest_distance_m,
         "route_efficiency": round(route_efficiency(r.shortest_distance_m, r.distance_m), 3) if r.shortest_distance_m else None,
         "traffic_delay_s": round(r.adjusted_duration_s - r.base_duration_s, 1), "active": r.active,
         "created_at": r.created_at, "superseded_at": r.superseded_at, "completed_at": r.completed_at,
         "reroute_of": str(r.reroute_of) if r.reroute_of else None, "reroute_reason": r.reroute_reason,
         "old_eta_s": r.old_eta_s, "time_saved_s": r.time_saved_s, "alternatives": r.alternatives, "geometry": coords}
    ar = ACTIVE.get(r.ambulance_id) if r.ambulance_id else None
    if ar and ar.route_id == r.id:
        d["progress_m"] = round(ar.progress_m, 1)
        d["eta_remaining_s"] = round(remaining_eta(ar), 1)
    if with_segments and db is not None:
        d["segments"] = [{"seq": s.seq, "road_id": s.road_id, "length_m": round(s.length_m, 1),
                          "base_speed_kph": s.base_speed_kph, "planned_speed_kph": round(s.planned_speed_kph, 1),
                          "cum_distance_m": round(s.cum_distance_m, 1)}
                         for s in db.scalars(select(RouteSegment).where(RouteSegment.route_id == r.id).order_by(RouteSegment.seq))]
    return d


@router.get("")
def list_routes(active: bool = True, limit: int = 200, _: User = Depends(any_user), db: Session = Depends(get_db)):
    q = select(Route).where(Route.leg != "PREVIEW")
    if active:
        q = q.where(Route.active.is_(True))
    return [route_dict(r) for r in db.scalars(q.order_by(Route.created_at.desc()).limit(limit))]


@router.get("/{route_id}")
def get_route(route_id: str, _: User = Depends(any_user), db: Session = Depends(get_db)):
    import uuid
    try:
        r = db.get(Route, uuid.UUID(route_id))
    except ValueError:
        r = None
    if r is None:
        raise HTTPException(404, "route not found")
    return route_dict(r, db, with_segments=True)


@router.post("/calculate")
def calculate(body: RouteRequest, _: User = Depends(any_user), db: Session = Depends(get_db)):
    """Traffic-aware route between two points (stored as a PREVIEW route)."""
    try:
        router_ = require_router()
    except RuntimeError as exc:
        raise HTTPException(503, f"routing unavailable: {exc}")
    o = (body.origin.latitude, body.origin.longitude)
    d = (body.destination.latitude, body.destination.longitude)
    try:
        rr = router_.route(o, d)
    except NoRouteError as exc:
        raise HTTPException(422, str(exc))
    r = save_route(db, rr, leg="PREVIEW", origin=o, dest=d)
    db.commit()
    out = route_dict(r, db, with_segments=True)
    out["origin_snap_m"], out["dest_snap_m"] = round(rr.origin_snap_m, 1), round(rr.dest_snap_m, 1)
    return out
