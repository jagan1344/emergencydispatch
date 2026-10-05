"""Route persistence, live route tracking and dynamic re-routing."""
from __future__ import annotations

import logging
import math
import threading
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Route, RouteSegment
from app.models.entities import linestring_wkt
from app.mqtt.client import publish
from app.routing.engine import NoRouteError, RouteResult, Segment
from app.routing.eta import degradation, time_saved_s
from app.services.events import after_commit, emit
from app.services.state import STATE, require_router
from app.utils.geo import interpolate
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.routes")


@dataclass
class ActiveRoute:
    route_id: uuid.UUID
    ambulance_id: str
    incident_id: uuid.UUID | None
    leg: str
    segments: list[Segment]           # segment.adj_speed_kph = speed assumed at planning time (baseline)
    dest: tuple[float, float]
    planned_eta_s: float
    progress_m: float = 0.0
    considered_roads: set[str] = field(default_factory=set)

    @property
    def total_m(self) -> float:
        return sum(s.length_m for s in self.segments)


class ActiveRoutes:
    def __init__(self):
        self._by_amb: dict[str, ActiveRoute] = {}
        self._lock = threading.RLock()

    def set(self, ar: ActiveRoute) -> None:
        with self._lock:
            self._by_amb[ar.ambulance_id] = ar

    def get(self, ambulance_id: str) -> ActiveRoute | None:
        return self._by_amb.get(ambulance_id)

    def pop(self, ambulance_id: str) -> ActiveRoute | None:
        with self._lock:
            return self._by_amb.pop(ambulance_id, None)

    def all(self) -> list[ActiveRoute]:
        with self._lock:
            return list(self._by_amb.values())

    def clear(self) -> None:
        with self._lock:
            self._by_amb.clear()


ACTIVE = ActiveRoutes()


# ------------------------------------------------------------------------------------------ persistence
def save_route(db: Session, rr: RouteResult, *, leg: str, origin: tuple[float, float], dest: tuple[float, float],
               ambulance_id: str | None = None, incident_id=None, dispatch_id=None, reroute_of=None,
               reroute_reason: str | None = None, old_eta_s: float | None = None) -> Route:
    route = Route(
        id=uuid.uuid4(), dispatch_id=dispatch_id, incident_id=incident_id, ambulance_id=ambulance_id, leg=leg,
        engine=rr.engine, network_source=rr.network_source, origin_lat=origin[0], origin_lon=origin[1],
        dest_lat=dest[0], dest_lon=dest[1], distance_m=rr.distance_m, base_duration_s=rr.base_duration_s,
        adjusted_duration_s=rr.adjusted_duration_s, osrm_duration_s=rr.osrm_duration_s,
        shortest_distance_m=rr.shortest_distance_m, geometry=linestring_wkt(rr.coords or [origin, dest]),
        alternatives=rr.alternatives, active=leg != "PREVIEW", reroute_of=reroute_of, reroute_reason=reroute_reason,
        old_eta_s=None if old_eta_s is None or math.isinf(old_eta_s) else old_eta_s,
        time_saved_s=(None if old_eta_s is None or math.isinf(old_eta_s)
                      else time_saved_s(old_eta_s, rr.adjusted_duration_s)),
    )
    db.add(route)
    db.flush()
    cum = 0.0
    rows = []
    for k, s in enumerate(rr.segments):
        cum += s.length_m
        rows.append(RouteSegment(route_id=route.id, seq=k, road_id=s.road_id, from_node=s.from_node,
                                 to_node=s.to_node, length_m=s.length_m, base_speed_kph=s.base_speed_kph,
                                 planned_speed_kph=s.adj_speed_kph, cum_distance_m=cum))
    db.add_all(rows)
    return route


def route_command(route: Route, rr: RouteResult, incident_id, leg: str) -> dict:
    """FOLLOW_ROUTE command sent to the ambulance simulator over MQTT."""
    return {
        "command": "FOLLOW_ROUTE", "route_id": str(route.id), "incident_id": str(incident_id) if incident_id else None,
        "leg": leg, "time_scale": get_settings().sim_time_scale,
        "origin": [route.origin_lat, route.origin_lon], "dest": [route.dest_lat, route.dest_lon],
        "segments": [{"road_id": s.road_id, "length_m": round(s.length_m, 2), "speed_kph": round(s.adj_speed_kph, 2),
                      "base_speed_kph": round(s.base_speed_kph, 2), "coords": [[round(c[0], 7), round(c[1], 7)] for c in s.coords]}
                     for s in rr.segments],
        "issued_at": utcnow().isoformat(),
    }


def activate(db: Session, route: Route, rr: RouteResult, ambulance_id: str, incident_id, leg: str,
             dest: tuple[float, float]) -> None:
    """Register the route as the ambulance's live route and send it to the simulator after commit."""
    ar = ActiveRoute(route.id, ambulance_id, incident_id, leg, list(rr.segments), dest, rr.adjusted_duration_s)
    cmd = route_command(route, rr, incident_id, leg)

    def _go():
        ACTIVE.set(ar)
        publish(f"ambulance/{ambulance_id}/command", cmd, retain=True)
    after_commit(db, _go)


def complete_route(db: Session, ambulance_id: str) -> None:
    ar = ACTIVE.pop(ambulance_id)
    if ar:
        db.execute(update(Route).where(Route.id == ar.route_id).values(active=False, completed_at=utcnow()))


def load_active_routes(db: Session) -> int:
    """Rebuild the in-memory cache from the database (after a backend restart)."""
    ACTIVE.clear()
    routes = db.scalars(select(Route).where(Route.active.is_(True), Route.ambulance_id.is_not(None))).all()
    for r in routes:
        segs = db.scalars(select(RouteSegment).where(RouteSegment.route_id == r.id).order_by(RouteSegment.seq)).all()
        segments = [Segment(s.road_id, s.from_node, s.to_node, s.length_m, s.base_speed_kph, s.planned_speed_kph, [])
                    for s in segs]
        _fill_coords(segments, r)
        ACTIVE.set(ActiveRoute(r.id, r.ambulance_id, r.incident_id, r.leg, segments, (r.dest_lat, r.dest_lon),
                               r.adjusted_duration_s))
    return len(routes)


def _fill_coords(segments: list[Segment], r: Route) -> None:
    from geoalchemy2.shape import to_shape
    pts = [(lat, lon) for lon, lat in to_shape(r.geometry).coords]
    g = STATE.graph
    for s in segments:
        if g is not None and s.from_node in g.idx_of and s.to_node in g.idx_of:
            a, b = g.idx_of[s.from_node], g.idx_of[s.to_node]
            s.coords = [(float(g.lat[a]), float(g.lon[a])), (float(g.lat[b]), float(g.lon[b]))]
    if segments and not segments[0].coords and pts:
        segments[0].coords = [pts[0], pts[min(1, len(pts) - 1)]]
    for s in segments:
        if not s.coords and pts:
            s.coords = [pts[-1], pts[-1]]


# ------------------------------------------------------------------------------------------ live progress
def position_on_route(ar: ActiveRoute, progress_m: float) -> tuple[int, float, tuple[float, float]]:
    """(segment index, fraction completed of that segment, interpolated point)."""
    cum = 0.0
    for k, s in enumerate(ar.segments):
        if cum + s.length_m >= progress_m or k == len(ar.segments) - 1:
            frac = 0.0 if s.length_m <= 0 else max(0.0, min(1.0, (progress_m - cum) / s.length_m))
            a, b = s.coords[0], s.coords[-1]
            return k, frac, interpolate(a[0], a[1], b[0], b[1], frac)
        cum += s.length_m
    return 0, 0.0, ar.dest


def remaining_eta(ar: ActiveRoute, current: bool = True) -> float:
    """Remaining traffic-adjusted ETA (s). The segment currently being driven is never treated as blocked:
    a closure prevents entering a road, the ambulance already on it can still leave."""
    router = require_router()
    rem, frac = router.remaining(ar.segments, ar.progress_m)
    if not rem:
        return 0.0
    segs = router.recost(rem) if current else rem
    first = segs[0]
    t_first = first.adj_time_s if first.adj_speed_kph > 0 else first.base_time_s
    return t_first * frac + sum(s.adj_time_s for s in segs[1:])


# ------------------------------------------------------------------------------------------ re-routing
def check_routes(affected_roads: set[str] | None = None, accident_roads: set[str] | None = None,
                 force_ambulance: str | None = None) -> list[dict]:
    """Check active routes against current traffic; recalculate when one of the triggers fires:
         * a road ahead is blocked
         * remaining ETA increased by more than REROUTE_THRESHOLD (default 20 %)
         * SEVERE congestion on a road ahead
         * an accident was reported on a road ahead
    """
    from app.database import session_scope

    results = []
    settings = get_settings()
    g = STATE.graph
    if g is None:
        return results
    for ar in ACTIVE.all():
        if force_ambulance and ar.ambulance_id != force_ambulance:
            continue
        router = require_router()
        rem, _ = router.remaining(ar.segments, ar.progress_m)
        ahead = rem[1:]
        ahead_roads = {s.road_id for s in ahead if s.road_id}
        if affected_roads is not None and not force_ambulance and not (ahead_roads & affected_roads):
            continue
        reason = None
        current = remaining_eta(ar, current=True)
        planned = remaining_eta(ar, current=False)
        deg = degradation(planned, current)
        blocked_ahead = [r for r in ahead_roads if g.road_state(r) and g.road_state(r).blocked]
        severe_ahead = [r for r in ahead_roads if g.road_state(r) and g.road_state(r).level == "SEVERE"
                        and r not in ar.considered_roads]
        acc_ahead = [r for r in ahead_roads & (accident_roads or set()) if r not in ar.considered_roads]
        if blocked_ahead:
            reason = f"road blocked ahead ({', '.join(sorted(blocked_ahead)[:3])})"
        elif acc_ahead:
            reason = f"accident on current route ({', '.join(sorted(acc_ahead)[:3])})"
        elif deg > settings.reroute_threshold:
            reason = f"ETA increased by {deg * 100:.0f}% (> {settings.reroute_threshold * 100:.0f}%)"
        elif severe_ahead:
            reason = f"severe congestion on current route ({', '.join(sorted(severe_ahead)[:3])})"
        elif force_ambulance:
            reason = "manual re-route request"
        if not reason:
            continue
        ar.considered_roads |= set(severe_ahead) | set(acc_ahead)
        with STATE.lock, session_scope() as db:
            if ACTIVE.get(ar.ambulance_id) is not ar:   # mission moved on meanwhile
                continue
            res = _reroute(db, ar, reason, current)
            results.append(res)
    return results


def _reroute(db: Session, ar: ActiveRoute, reason: str, old_eta: float) -> dict:
    router = require_router()
    g = STATE.graph
    k, frac, point = position_on_route(ar, ar.progress_m)
    seg = ar.segments[k]
    prefix: list[Segment] = []
    origin_node = None
    if seg.to_node is not None and seg.to_node in g.idx_of:
        left = seg.length_m * (1 - frac)
        if left > 0.5:
            prefix.append(Segment(seg.road_id, seg.from_node, seg.to_node, left, seg.base_speed_kph,
                                  seg.adj_speed_kph if seg.adj_speed_kph > 0 else seg.base_speed_kph,
                                  [point, seg.coords[-1]]))
            prefix = router.recost(prefix)
            if prefix[0].adj_speed_kph <= 0:  # already on the blocked road: allowed to leave at base speed
                p = prefix[0]
                prefix = [Segment(p.road_id, p.from_node, p.to_node, p.length_m, p.base_speed_kph, p.base_speed_kph, p.coords)]
        origin_node = g.idx_of[seg.to_node]
    payload = {"ambulance_id": ar.ambulance_id, "incident_id": str(ar.incident_id) if ar.incident_id else None,
               "reason": reason, "old_route_id": str(ar.route_id), "leg": ar.leg,
               "old_eta_s": None if math.isinf(old_eta) else round(old_eta, 1)}
    try:
        new = router.route(point, ar.dest, origin_node=origin_node, prefix=prefix)
    except NoRouteError as exc:
        emit(db, "ROUTE_CHECK", {**payload, "decision": "NO_ALTERNATIVE", "detail": str(exc)},
             incident_id=ar.incident_id, ambulance_id=ar.ambulance_id)
        return {**payload, "decision": "NO_ALTERNATIVE"}
    new_eta = new.adjusted_duration_s
    same_path = [s.road_id for s in new.segments if s.road_id] == [
        s.road_id for s in router.remaining(ar.segments, ar.progress_m)[0] if s.road_id]
    min_gain = max(10.0, 0.05 * old_eta) if math.isfinite(old_eta) else 0.0
    if same_path or (math.isfinite(old_eta) and new_eta > old_eta - min_gain):
        # keep current route; accept the new conditions as the baseline so we do not re-trigger every tick
        ar.segments = router.recost(ar.segments)
        emit(db, "ROUTE_CHECK", {**payload, "decision": "KEEP_CURRENT", "best_alternative_eta_s": round(new_eta, 1)},
             incident_id=ar.incident_id, ambulance_id=ar.ambulance_id)
        log_event(log, "ROUTE_CHECK_KEEP", ambulance_id=ar.ambulance_id, reason=reason, old_eta_s=old_eta, alt_eta_s=new_eta)
        return {**payload, "decision": "KEEP_CURRENT", "new_eta_s": new_eta}

    from app.models import Route as RouteModel
    old = db.get(RouteModel, ar.route_id)
    if old is not None:
        old.active = False
        old.superseded_at = utcnow()
    route = save_route(db, new, leg=ar.leg, origin=point, dest=ar.dest, ambulance_id=ar.ambulance_id,
                       incident_id=ar.incident_id, dispatch_id=old.dispatch_id if old else None,
                       reroute_of=ar.route_id, reroute_reason=reason, old_eta_s=old_eta)
    saved = None if math.isinf(old_eta) else round(old_eta - new_eta, 1)
    data = {**payload, "decision": "REROUTED", "new_route_id": str(route.id), "new_eta_s": round(new_eta, 1),
            "time_saved_s": saved, "engine": new.engine, "distance_m": round(new.distance_m, 1),
            "geometry": [[round(a, 6), round(b, 6)] for a, b in new.coords]}
    emit(db, "ROUTE_RECALCULATED", data, incident_id=ar.incident_id, ambulance_id=ar.ambulance_id)
    activate(db, route, new, ar.ambulance_id, ar.incident_id, ar.leg, ar.dest)
    from app.services import metrics
    metrics.REROUTES.inc()
    log_event(log, "ROUTE_RECALCULATED", ambulance_id=ar.ambulance_id, reason=reason,
              old_eta_s=None if math.isinf(old_eta) else round(old_eta, 1), new_eta_s=round(new_eta, 1),
              time_saved_s=saved)
    return data
