"""Traffic state management. Single entry point apply_update() keeps PostGIS, the in-memory graph,
the event log, MQTT subscribers and WebSocket clients consistent, then triggers the route monitor."""
from __future__ import annotations

import logging
import random
import threading

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import RoadCondition, TrafficEvent
from app.models.entities import point_wkt
from app.mqtt.client import publish
from app.routing.traffic import CONGESTION_LEVELS, LEVEL_DENSITY, adjusted_speed_mps
from app.services.events import after_commit, emit
from app.services.state import STATE
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.traffic")
ACCIDENT_ROADS: set[str] = set()      # roads with an active (uncleared) accident
_accident_lock = threading.Lock()


class TrafficError(ValueError):
    pass


def road_dict(r: RoadCondition) -> dict:
    return {"road_id": r.road_id, "name": r.name, "highway_type": r.highway_type,
            "speed_limit_kph": r.speed_limit_kph, "current_speed_kph": round(r.current_speed_kph, 1),
            "congestion_level": r.congestion_level, "blocked": r.blocked,
            "incident_multiplier": r.incident_multiplier, "vehicle_density": r.vehicle_density,
            "length_m": round(r.length_m, 1), "accident": r.road_id in ACCIDENT_ROADS}


def apply_update(db: Session, road_id: str, *, level: str | None = None, blocked: bool | None = None,
                 incident_multiplier: float | None = None, event_type: str = "CONGESTION", source: str = "DISPATCHER",
                 location: tuple[float, float] | None = None, details: dict | None = None,
                 publish_mqtt: bool = True) -> dict | None:
    road = db.get(RoadCondition, road_id, with_for_update=True)
    if road is None:
        raise TrafficError(f"unknown road_id {road_id}")
    if level is not None and level not in CONGESTION_LEVELS:
        raise TrafficError(f"invalid congestion level {level}")
    new_level = level or road.congestion_level
    if blocked is False and new_level == "BLOCKED":
        new_level = "HEAVY"                     # unblocking a road leaves residual congestion
    new_blocked = road.blocked if blocked is None else blocked
    if new_level == "BLOCKED":
        new_blocked = True
    if new_blocked:
        new_level = "BLOCKED"
    new_mult = road.incident_multiplier if incident_multiplier is None else incident_multiplier
    if not 0 < new_mult <= 1:
        raise TrafficError("incident_multiplier must be in (0, 1]")
    if (new_level, new_blocked, round(new_mult, 3)) == (road.congestion_level, road.blocked, round(road.incident_multiplier, 3)) \
            and event_type not in ("ACCIDENT", "CLEAR"):
        return None  # idempotent: nothing changed (e.g. our own MQTT message echoed back)
    old_level = road.congestion_level
    road.congestion_level = new_level
    road.blocked = new_blocked
    road.incident_multiplier = new_mult
    road.current_speed_kph = adjusted_speed_mps(road.speed_limit_kph, new_level, new_mult, new_blocked) * 3.6
    road.vehicle_density = LEVEL_DENSITY[new_level]
    road.updated_at = utcnow()
    with _accident_lock:
        if event_type == "ACCIDENT":
            ACCIDENT_ROADS.add(road_id)
        elif event_type in ("CLEAR", "UNBLOCK") or (new_level == "FREE" and not new_blocked):
            ACCIDENT_ROADS.discard(road_id)
    ev = TrafficEvent(road_id=road_id, event_type=event_type, old_level=old_level, new_level=new_level,
                      blocked=new_blocked, incident_multiplier=new_mult, source=source,
                      location=point_wkt(*location) if location else None, details=details or {})
    db.add(ev)
    if STATE.graph is not None:
        STATE.graph.set_road_state(road_id, new_level, new_mult, new_blocked)
    data = {**road_dict(road), "event_type": event_type, "old_level": old_level, "source": source}
    emit(db, "TRAFFIC_CHANGED", data)
    log_event(log, "TRAFFIC_EVENT", road_id=road_id, event_type=event_type, old_level=old_level, new_level=new_level,
              blocked=new_blocked, source=source)
    status_msg = {"road_id": road_id, "congestion_level": new_level, "blocked": new_blocked,
                  "incident_multiplier": new_mult, "speed_limit_kph": road.speed_limit_kph,
                  "current_speed_kph": round(road.current_speed_kph, 2), "source": source, "event_type": event_type}
    is_accident = event_type == "ACCIDENT"

    def _after():
        if publish_mqtt:
            publish(f"traffic/{road_id}/status", status_msg, retain=True)
            if source != "SIMULATOR":
                publish("traffic/events", {**status_msg, "ts": utcnow().isoformat()})
        _trigger_route_check({road_id}, {road_id} if is_accident else set())
    after_commit(db, _after)
    return data


def _trigger_route_check(roads: set[str], accident_roads: set[str]) -> None:
    from app.services.routes_service import check_routes

    def _run():
        try:
            check_routes(affected_roads=roads, accident_roads=accident_roads | (roads & ACCIDENT_ROADS))
        except Exception:
            log.exception("route check failed")
    threading.Thread(target=_run, daemon=True, name="route-check").start()


def roads_near(db: Session, lat: float, lon: float, radius_m: float = 60, limit: int = 3) -> list[str]:
    """Roads within radius of a point, closest first (PostGIS ST_DWithin + KNN ordering)."""
    rows = db.execute(text(
        "SELECT road_id FROM road_conditions "
        "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography, :r) "
        "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography LIMIT :n"),
        {"lat": lat, "lon": lon, "r": radius_m, "n": limit}).all()
    return [r[0] for r in rows]


def nearest_road(db: Session, lat: float, lon: float) -> dict | None:
    row = db.execute(text(
        "SELECT road_id, ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography) AS d "
        "FROM road_conditions ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography LIMIT 1"),
        {"lat": lat, "lon": lon}).first()
    if not row:
        return None
    road = db.get(RoadCondition, row[0])
    return {**road_dict(road), "distance_m": round(row[1], 1)}


EVENT_PRESETS = {
    # event_type: (level, blocked, incident_multiplier)
    "ACCIDENT": ("SEVERE", False, 0.5),
    "BLOCK": ("BLOCKED", True, 1.0),
    "UNBLOCK": ("FREE", False, 1.0),
    "CLEAR": ("FREE", False, 1.0),
}


def create_event(db: Session, event_type: str, *, road_id: str | None = None, lat: float | None = None,
                 lon: float | None = None, level: str | None = None, radius_m: float = 60,
                 source: str = "DISPATCHER") -> list[dict]:
    """Dispatcher / scenario traffic event on a road (or every road within radius_m of a point)."""
    if road_id:
        roads = [road_id]
    elif lat is not None and lon is not None:
        roads = roads_near(db, lat, lon, radius_m)
        if not roads:
            raise TrafficError(f"no road within {radius_m:.0f} m of ({lat:.5f}, {lon:.5f})")
    else:
        raise TrafficError("road_id or lat/lon required")
    out = []
    for rid in roads:
        if event_type == "CONGESTION":
            if level is None:
                raise TrafficError("level required for CONGESTION events")
            res = apply_update(db, rid, level=level, blocked=(level == "BLOCKED"), event_type="CONGESTION", source=source,
                               location=(lat, lon) if lat is not None else None)
        elif event_type in EVENT_PRESETS:
            lv, bl, mult = EVENT_PRESETS[event_type]
            res = apply_update(db, rid, level=lv, blocked=bl, incident_multiplier=mult, event_type=event_type,
                               source=source, location=(lat, lon) if lat is not None else None)
            if event_type in ("CLEAR", "UNBLOCK"):
                db.execute(text("UPDATE traffic_events SET active=false, cleared_at=now() WHERE road_id=:r AND active"),
                           {"r": rid})
        else:
            raise TrafficError(f"unknown event type {event_type}")
        if res:
            out.append(res)
    return out


def simulate_step(db: Session, rng: random.Random, n_changes: int = 5, source: str = "SIMULATION") -> list[dict]:
    """One step of server-side random traffic dynamics (used by simulation mode / POST /traffic/simulate)."""
    roads = db.scalars(select(RoadCondition)).all()
    if not roads:
        return []
    weights = [3.0 if r.highway_type in ("primary", "secondary", "trunk", "motorway", "tertiary") else 1.0 for r in roads]
    changes = []
    for road in rng.choices(roads, weights=weights, k=n_changes):
        idx = CONGESTION_LEVELS.index(road.congestion_level)
        roll = rng.random()
        if road.blocked or road.road_id in ACCIDENT_ROADS:
            if roll < 0.5:
                changes.append(apply_update(db, road.road_id, level="MODERATE", blocked=False, incident_multiplier=1.0,
                                            event_type="CLEAR", source=source))
            continue
        if roll < 0.06:
            changes.append(apply_update(db, road.road_id, level="SEVERE", incident_multiplier=0.5,
                                        event_type="ACCIDENT", source=source))
        elif roll < 0.09:
            changes.append(apply_update(db, road.road_id, level="BLOCKED", blocked=True, event_type="BLOCK", source=source))
        else:
            step = rng.choice([-1, -1, 1, 1, 2]) if idx < 4 else rng.choice([-1, -2])
            new = CONGESTION_LEVELS[max(0, min(4, idx + step))]
            changes.append(apply_update(db, road.road_id, level=new, event_type="CONGESTION", source=source))
    return [c for c in changes if c]


def reset_all(db: Session, source: str = "DISPATCHER") -> int:
    rows = db.scalars(select(RoadCondition).where(
        (RoadCondition.congestion_level != "FREE") | RoadCondition.blocked | (RoadCondition.incident_multiplier < 1))).all()
    for r in rows:
        apply_update(db, r.road_id, level="FREE", blocked=False, incident_multiplier=1.0, event_type="CLEAR", source=source)
    db.execute(text("UPDATE traffic_events SET active=false, cleared_at=now() WHERE active"))
    return len(rows)
