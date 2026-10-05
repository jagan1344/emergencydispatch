import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import check_database
from app.services.state import STATE

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health():
    """Component health. HTTP 503 when the database or routing graph is down."""
    db = check_database()
    s = get_settings()
    now = time.time()
    osrm = STATE.osrm.status() if STATE.osrm else {"configured": False}
    if STATE.osrm and STATE.osrm.enabled:
        osrm["reachable"] = STATE.osrm.health()
    mqtt = STATE.mqtt.status() if STATE.mqtt else {"connected": False, "enabled": s.mqtt_enabled}
    body = {
        "status": "ok" if db["ok"] and STATE.graph is not None else "degraded",
        "database": db,
        "routing": {"graph": STATE.graph.stats() if STATE.graph else None, "error": STATE.graph_error, "osrm": osrm,
                    "mode": ("osm+osrm" if osrm.get("reachable") else "osm-graph") if STATE.graph and STATE.graph.source == "osm"
                    else "synthetic-fallback" if STATE.graph else "unavailable"},
        "ml_model": {"available": STATE.model.available, "version": STATE.model.version, "error": STATE.model.error},
        "mqtt": mqtt,
        "simulator": {"ambulance_sim_connected": STATE.simulator_last_seen is not None and now - STATE.simulator_last_seen < 10,
                      "traffic_sim_connected": STATE.traffic_sim_last_seen is not None and now - STATE.traffic_sim_last_seen < 15},
        "websocket_clients": len(STATE.ws.connections),
        "city": {"name": s.city_name, "lat": s.city_lat, "lon": s.city_lon, "radius_m": s.city_radius_m},
        "sim_time_scale": s.sim_time_scale,
    }
    return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)
