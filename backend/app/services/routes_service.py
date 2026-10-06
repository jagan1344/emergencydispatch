"""Route persistence, live route tracking and dynamic re-routing."""
from __future__ import annotations

import logging
import math
import threading
import time
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
    last_reroute_at: float | None = None                 # wall time.time() of the last re-route
    abandoned: list[frozenset] = field(default_factory=list)   # road sets of recently abandoned routes
    reroutes: int = 0

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
        alternatives=rr.alternatives, active=leg != "PREVIEW",
        predicted_duration_s=(rr.predicted_duration_s if rr.predicted_duration_s is not None
                              and math.isfinite(rr.predicted_duration_s) else None),
        prediction_horizon_min=rr.prediction_horizon_min, reroute_of=reroute_of, reroute_reason=reroute_reason,
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


def route_command(route: Route, rr: RouteResult, incident_id, leg: str, update: bool = False) -> dict:
    """FOLLOW_ROUTE (new mission leg) or ROUTE_UPDATED (re-route of the current leg: the simulator continues from
    its current GPS position on the new route instead of restarting at the route origin) over MQTT."""
    return {
        "command": "ROUTE_UPDATED" if update else "FOLLOW_ROUTE", "route_id": str(route.id),
        "replaces_route_id": str(route.reroute_of) if update and route.reroute_of else None,
        "geometry": [[round(a, 6), round(b, 6)] for a, b in rr.coords], "incident_id": str(incident_id) if incident_id else None,
        "leg": leg, "time_scale": get_settings().sim_time_scale,
        "origin": [route.origin_lat, route.origin_lon], "dest": [route.dest_lat, route.dest_lon],
        "segments": [{"road_id": s.road_id, "length_m": round(s.length_m, 2), "speed_kph": round(s.adj_speed_kph, 2),
                      "base_speed_kph": round(s.base_speed_kph, 2), "coords": [[round(c[0], 7), round(c[1], 7)] for c in s.coords]}
                     for s in rr.segments],
        "issued_at": utcnow().isoformat(),
    }


def activate(db: Session, route: Route, rr: RouteResult, ambulance_id: str, incident_id, leg: str,
             dest: tuple[float, float], previous: "ActiveRoute | None" = None) -> None:
    """Register the route as the ambulance's live route and send it to the simulator after commit."""
    ar = ActiveRoute(route.id, ambulance_id, incident_id, leg, list(rr.segments), dest, rr.adjusted_duration_s)
    if previous is not None:          # keep re-route history for cooldown / oscillation control
        ar.last_reroute_at = time.time()
        ar.reroutes = previous.reroutes + 1
        ar.abandoned = (previous.abandoned + [frozenset(s.road_id for s in previous.segments if s.road_id)])[-5:]
    cmd = route_command(route, rr, incident_id, leg, update=previous is not None)
    from app.models import Ambulance
    amb = db.get(Ambulance, ambulance_id)
    cmd["driver"] = amb.gps_source if amb is not None else "SIMULATED"   # simulator ignores DEVICE units

    def _go():
        ACTIVE.set(ar)
        publish(f"ambulance/{ambulance_id}/command", cmd, retain=True)
    after_commit(db, _go)


def emit_route_selected(db: Session, rr: RouteResult, leg: str, incident_id, ambulance_id: str, route_id) -> None:
    eta = rr.predicted_duration_s
    emit(db, "ROUTE_SELECTED", {
        "incident_id": str(incident_id) if incident_id else None, "ambulance_id": ambulance_id, "route_id": str(route_id),
        "leg": leg, "engine": rr.engine, "candidates": len(rr.alternatives), "alternatives": rr.alternatives,
        "eta_s": round(rr.adjusted_duration_s, 1),
        "predicted_eta_s": round(eta, 1) if eta is not None and math.isfinite(eta) else None,
        "horizon_min": rr.prediction_horizon_min, "distance_m": round(rr.distance_m, 1)},
        incident_id=incident_id, ambulance_id=ambulance_id)


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


def predicted_remaining_eta(ar: ActiveRoute) -> float | None:
    """Remaining ETA under predicted traffic (time-dependent blend, see routing.engine.predicted_duration)."""
    st = get_settings()
    if not st.traffic_prediction_enabled:
        return None
    from app.routing.engine import predicted_duration
    router = require_router()
    rem, frac = router.remaining(ar.segments, ar.progress_m)
    if not rem:
        return 0.0
    return predicted_duration(router.recost(rem), st.traffic_prediction_horizon_min * 60, first_fraction=frac)


def decision_eta(ar: ActiveRoute) -> float:
    """ETA metric used for re-route decisions: predicted if enabled & finite, else current."""
    p = predicted_remaining_eta(ar)
    return p if p is not None and math.isfinite(p) else remaining_eta(ar)


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
        current = remaining_eta(ar, current=True)
        planned = remaining_eta(ar, current=False)
        predicted = predicted_remaining_eta(ar)
        trig = reroute_trigger(ahead_roads, g.road_state, planned, current, predicted, ar.considered_roads,
                               accident_roads, settings)
        reason, blocked_ahead = trig.reason, trig.blocked_ahead
        if reason is None and force_ambulance:
            reason = "manual re-route request"
        if not reason:
            continue
        ar.considered_roads |= trig.mark_considered
        with STATE.lock, session_scope() as db:
            if ACTIVE.get(ar.ambulance_id) is not ar:   # mission moved on meanwhile
                continue
            emit(db, "ROUTE_DEGRADATION_DETECTED", {
                "ambulance_id": ar.ambulance_id, "incident_id": str(ar.incident_id) if ar.incident_id else None,
                "route_id": str(ar.route_id), "reason": reason, "planned_eta_s": round(planned, 1),
                "current_eta_s": None if math.isinf(current) else round(current, 1),
                "predicted_eta_s": None if predicted is None or math.isinf(predicted) else round(predicted, 1)},
                incident_id=ar.incident_id, ambulance_id=ar.ambulance_id)
            log_event(log, "ROUTE_DEGRADATION_DETECTED", ambulance_id=ar.ambulance_id, route_id=str(ar.route_id),
                      incident_id=str(ar.incident_id), reason=reason)
            res = _reroute(db, ar, reason, decision_eta(ar) if not blocked_ahead else math.inf,
                           forced=bool(blocked_ahead) or bool(force_ambulance))
            results.append(res)
    return results


@dataclass
class RerouteTrigger:
    reason: str | None
    blocked_ahead: list[str]
    mark_considered: set[str]          # roads (and the "predicted" marker) not to trigger again on this route


def reroute_trigger(ahead_roads: set[str], road_state, planned: float, current: float, predicted: float | None,
                    considered: set[str], accident_roads: set[str] | None, st) -> RerouteTrigger:
    """Pure trigger rule (shared by the live route monitor and the evaluation simulator). First match wins:
         road blocked ahead > accident on the route > ETA +REROUTE_THRESHOLD > SEVERE congestion ahead
         > predicted ETA +REROUTE_THRESHOLD within the prediction horizon."""
    deg = degradation(planned, current)
    pdeg = degradation(planned, predicted) if predicted is not None else 0.0
    blocked_ahead = [r for r in ahead_roads if road_state(r) and road_state(r).blocked]
    severe_ahead = [r for r in ahead_roads if road_state(r) and road_state(r).level == "SEVERE" and r not in considered]
    acc_ahead = [r for r in ahead_roads & (accident_roads or set()) if r not in considered]
    mark = set(severe_ahead) | set(acc_ahead)
    reason = None
    if blocked_ahead:
        reason = f"road blocked ahead ({', '.join(sorted(blocked_ahead)[:3])})"
    elif acc_ahead:
        reason = f"accident on current route ({', '.join(sorted(acc_ahead)[:3])})"
    elif deg > st.reroute_threshold:
        reason = f"ETA increased by {deg * 100:.0f}% (> {st.reroute_threshold * 100:.0f}%)"
    elif severe_ahead:
        reason = f"severe congestion on current route ({', '.join(sorted(severe_ahead)[:3])})"
    elif pdeg > st.reroute_threshold and "predicted" not in considered:
        reason = (f"traffic predicted to worsen on current route: ETA +{pdeg * 100:.0f}% within "
                  f"{st.traffic_prediction_horizon_min:.0f} min")
        mark.add("predicted")
    return RerouteTrigger(reason, blocked_ahead, mark if reason else set())


def reroute_decision(old_eta: float, new_eta: float, *, since_last_s: float | None, similarity: float,
                     forced: bool, st=None) -> tuple[bool, str]:
    """Pure re-route rule (unit-tested). Returns (accept, explanation).
      accept iff  new_eta + max(MIN_ETA_SAVINGS, old_eta * MIN_IMPROVEMENT%) <= old_eta
              and (no re-route within COOLDOWN, or the current route is blocked)
              and the new route is not (almost) a route we abandoned recently (oscillation), unless blocked."""
    st = st or get_settings()
    if math.isinf(new_eta):
        return False, "alternative is not drivable"
    if math.isinf(old_eta):
        return True, "current route is blocked: any drivable alternative is accepted"
    margin = max(st.reroute_min_eta_savings_s, old_eta * st.reroute_min_improvement_percent / 100)
    if new_eta + margin > old_eta:
        return False, (f"saving {old_eta - new_eta:.0f} s is below the required margin {margin:.0f} s "
                       f"(min {st.reroute_min_eta_savings_s:.0f} s / {st.reroute_min_improvement_percent:.0f}%)")
    if not forced and since_last_s is not None and since_last_s < st.reroute_cooldown_s:
        return False, f"cooldown: last re-route {since_last_s:.0f} s ago (< {st.reroute_cooldown_s:.0f} s)"
    if not forced and similarity >= st.reroute_oscillation_similarity:
        return False, (f"oscillation guard: alternative is {similarity:.0%} identical to a route abandoned recently")
    return True, f"saves {old_eta - new_eta:.0f} s (margin {margin:.0f} s)"


def _similarity(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _reroute(db: Session, ar: ActiveRoute, reason: str, old_eta: float, forced: bool = False) -> dict:
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
    new_eta = new.eta_s
    same_path = [s.road_id for s in new.segments if s.road_id] == [
        s.road_id for s in router.remaining(ar.segments, ar.progress_m)[0] if s.road_id]
    new_roads = frozenset(s.road_id for s in new.segments if s.road_id)
    sim = max((_similarity(new_roads, old) for old in ar.abandoned), default=0.0)
    since = None if ar.last_reroute_at is None else (time.time() - ar.last_reroute_at) * get_settings().sim_time_scale
    ok, why = (False, "best alternative is the current route") if same_path else \
        reroute_decision(old_eta, new_eta, since_last_s=since, similarity=sim, forced=forced)
    log_event(log, "REROUTE_EVALUATED", ambulance_id=ar.ambulance_id, route_id=str(ar.route_id),
              incident_id=str(ar.incident_id), old_eta_s=None if math.isinf(old_eta) else round(old_eta, 1),
              alt_eta_s=round(new_eta, 1) if math.isfinite(new_eta) else None, accept=ok, rule=why)
    if not ok:
        # keep current route; accept the new conditions as the baseline so we do not re-trigger every tick
        ar.segments = router.recost(ar.segments)
        emit(db, "ROUTE_CHECK", {**payload, "decision": "KEEP_CURRENT", "detail": why,
                                 "best_alternative_eta_s": round(new_eta, 1) if math.isfinite(new_eta) else None},
             incident_id=ar.incident_id, ambulance_id=ar.ambulance_id)
        return {**payload, "decision": "KEEP_CURRENT", "new_eta_s": new_eta, "rule": why}
    log_event(log, "REROUTE_TRIGGERED", ambulance_id=ar.ambulance_id, route_id=str(ar.route_id),
              incident_id=str(ar.incident_id), reason=reason, rule=why)

    return _apply_new_route(db, ar, new, point, reason, old_eta, payload)


def _apply_new_route(db: Session, ar: ActiveRoute, new: RouteResult, point: tuple[float, float], reason: str,
                     old_eta: float, payload: dict) -> dict:
    from app.models import Route as RouteModel
    new_eta = new.eta_s
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
    activate(db, route, new, ar.ambulance_id, ar.incident_id, ar.leg, ar.dest, previous=ar)
    log_event(log, "ROUTE_UPDATED", ambulance_id=ar.ambulance_id, route_id=str(route.id),
              replaces=str(ar.route_id), incident_id=str(ar.incident_id))
    from app.services import metrics
    metrics.REROUTES.inc()
    log_event(log, "ROUTE_RECALCULATED", ambulance_id=ar.ambulance_id, reason=reason,
              old_eta_s=None if math.isinf(old_eta) else round(old_eta, 1), new_eta_s=round(new_eta, 1),
              time_saved_s=saved)
    return data


def reroute_from_point(db: Session, ar: ActiveRoute, point: tuple[float, float], reason: str) -> dict | None:
    """New route from an arbitrary position (e.g. a real GPS fix that left the planned route)."""
    router = require_router()
    try:
        new = router.route(point, ar.dest)
    except NoRouteError:
        return None
    payload = {"ambulance_id": ar.ambulance_id, "incident_id": str(ar.incident_id) if ar.incident_id else None,
               "reason": reason, "old_route_id": str(ar.route_id), "leg": ar.leg, "old_eta_s": None}
    return _apply_new_route(db, ar, new, point, reason, math.inf, payload)
