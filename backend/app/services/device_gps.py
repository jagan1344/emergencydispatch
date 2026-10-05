"""Real GPS from a crew device (phone browser / vehicle tracker) instead of the simulator.

Each fix is map-matched onto the active route (nearest segment ahead of the last known progress) to obtain
route progress and remaining ETA, exactly like simulator fixes. Arrival is detected when the device is
within ARRIVAL_RADIUS_M of the destination (or confirmed manually by the crew). If the device is more than
OFF_ROUTE_M away from the planned route for OFF_ROUTE_FIXES consecutive fixes, a new route is computed from
the device's real position.
"""
from __future__ import annotations

import logging
import math
import threading

from app.database import session_scope
from app.models import Ambulance
from app.mqtt.client import publish
from app.services.mission_service import handle_sim_status
from app.services.routes_service import ACTIVE, ActiveRoute, reroute_from_point
from app.services.state import STATE
from app.services.telemetry_service import TELEMETRY
from app.utils.geo import haversine_m
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.device_gps")
ARRIVAL_RADIUS_M = 40.0
OFF_ROUTE_M = 100.0
OFF_ROUTE_FIXES = 2
_off_route: dict[str, int] = {}
_last_fix: dict[str, tuple[float, float, float]] = {}
_lock = threading.Lock()


def project(ar: ActiveRoute, lat: float, lon: float) -> tuple[float, float]:
    """(progress along route in m, distance from route in m). Searches from slightly behind the last
    progress so GPS noise cannot make progress jump backwards by much."""
    kx = 111_320.0 * math.cos(math.radians(lat))
    ky = 110_540.0
    best = (ar.progress_m, math.inf)
    cum = 0.0
    for s in ar.segments:
        start = cum
        cum += s.length_m
        if cum < ar.progress_m - 100 or len(s.coords) < 2:
            continue
        (alat, alon), (blat, blon) = s.coords[0], s.coords[-1]
        ax, ay = (alon - lon) * kx, (alat - lat) * ky
        bx, by = (blon - lon) * kx, (blat - lat) * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best[1]:
            best = (start + t * s.length_m, d)
    return best


def handle_fix(ambulance_id: str, lat: float, lon: float, speed_kph: float | None = None,
               accuracy_m: float | None = None) -> dict:
    now = utcnow()
    with _lock:
        prev = _last_fix.get(ambulance_id)
        _last_fix[ambulance_id] = (lat, lon, now.timestamp())
    if speed_kph is None and prev is not None and now.timestamp() > prev[2]:
        speed_kph = haversine_m(prev[0], prev[1], lat, lon) / (now.timestamp() - prev[2]) * 3.6
    speed_kph = max(0.0, min(250.0, speed_kph or 0.0))

    with STATE.lock, session_scope() as db:
        amb = db.get(Ambulance, ambulance_id, with_for_update=True)
        if amb is None:
            raise KeyError(ambulance_id)
        if amb.gps_source != "DEVICE":
            amb.gps_source = "DEVICE"
            publish(f"ambulance/{ambulance_id}/command", {"command": "IDLE", "reason": "device GPS took over"}, retain=True)
            log_event(log, "DEVICE_GPS_STARTED", ambulance_id=ambulance_id)
        amb.gps_accuracy_m = accuracy_m
        ar = ACTIVE.get(ambulance_id)
        result = {"ambulance_id": ambulance_id, "status": amb.status, "route_id": None, "progress_m": None,
                  "off_route_m": None, "arrived": False}
        if ar is None:
            if amb.status == "AVAILABLE":   # idle units: the device position is the unit's position
                amb.latitude, amb.longitude = lat, lon
        else:
            progress, off = project(ar, lat, lon)
            result.update(route_id=str(ar.route_id), progress_m=round(progress, 1), off_route_m=round(off, 1))
            if amb.status == "DISPATCHED" and ar.leg == "TO_PATIENT":
                handle_sim_status(db, ambulance_id, {"event": "ROUTE_STARTED", "route_id": str(ar.route_id)})
            if haversine_m(lat, lon, *ar.dest) <= ARRIVAL_RADIUS_M:
                handle_sim_status(db, ambulance_id, {"event": "ARRIVED", "route_id": str(ar.route_id)})
                result["arrived"] = True
            elif off > OFF_ROUTE_M:
                n = _off_route.get(ambulance_id, 0) + 1
                _off_route[ambulance_id] = n
                if n >= OFF_ROUTE_FIXES:
                    _off_route[ambulance_id] = 0
                    res = reroute_from_point(db, ar, (lat, lon), f"vehicle left planned route ({off:.0f} m off)")
                    result["rerouted"] = bool(res)
            else:
                _off_route[ambulance_id] = 0
                ar.progress_m = max(ar.progress_m, progress) if progress >= ar.progress_m - 30 else progress
    # live pipeline identical to simulator fixes (WebSocket push + batched PostGIS writes)
    cur = ACTIVE.get(ambulance_id)
    TELEMETRY.handle_location(ambulance_id, {
        "latitude": lat, "longitude": lon, "speed": speed_kph, "timestamp": now.isoformat(),
        "route_id": str(cur.route_id) if cur else None, "progress_m": cur.progress_m if cur else None})
    publish(f"ambulance/{ambulance_id}/location", {"ambulance_id": ambulance_id, "latitude": lat, "longitude": lon,
                                                   "speed": round(speed_kph, 1), "accuracy_m": accuracy_m,
                                                   "timestamp": now.isoformat(), "source": "DEVICE"})
    return result


def confirm_arrival(ambulance_id: str) -> bool:
    ar = ACTIVE.get(ambulance_id)
    if ar is None:
        return False
    with STATE.lock, session_scope() as db:
        handle_sim_status(db, ambulance_id, {"event": "ARRIVED", "route_id": str(ar.route_id)})
    return True


def release(ambulance_id: str) -> None:
    """Hand the unit back to the simulator."""
    with STATE.lock, session_scope() as db:
        amb = db.get(Ambulance, ambulance_id, with_for_update=True)
        if amb is not None:
            amb.gps_source = "SIMULATED"
            amb.gps_accuracy_m = None
