"""FastAPI application: REST API, WebSocket /ws, Prometheus /metrics and background workers."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api import analytics, auth, emergencies, fleet, health, ml, routes, simulation, traffic
from app.config import get_settings
from app.database import get_engine, run_migrations, session_scope
from app.routing.engine import RoutingEngine
from app.routing.graph import GraphNotLoaded, RoadGraph
from app.routing.osrm_client import OsrmClient
from app.services import metrics
from app.services.auth_service import decode_token
from app.services.state import STATE
from app.utils.logging import configure_logging, log_event

log = logging.getLogger("app.main")


def init_runtime() -> None:
    """Load model, road graph, routing engine and active routes (synchronous; also used by tests)."""
    s = get_settings()
    STATE.model.load()
    STATE.osrm = OsrmClient(s.osrm_url, s.osrm_timeout_s) if s.osrm_url else None
    try:
        STATE.graph = RoadGraph.load(get_engine())
        STATE.router = RoutingEngine(STATE.graph, STATE.osrm)
        STATE.graph_error = None
        log_event(log, "ROAD_GRAPH_LOADED", **STATE.graph.stats())
    except GraphNotLoaded as exc:
        STATE.graph, STATE.router, STATE.graph_error = None, None, str(exc)
        log.error(str(exc), extra={"event": "ROAD_GRAPH_MISSING"})
    if STATE.router is not None:
        from app.services.routes_service import load_active_routes
        with session_scope() as db:
            n = load_active_routes(db)
        log_event(log, "ACTIVE_ROUTES_RESTORED", count=n)


def publish_traffic_snapshot() -> None:
    """On (re)connect, publish the current state of every non-free road as retained MQTT messages so that
    simulators started later share the backend's traffic picture."""
    from app.mqtt.client import publish
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT road_id, congestion_level, blocked, incident_multiplier, speed_limit_kph, "
                                 "current_speed_kph FROM road_conditions WHERE congestion_level <> 'FREE' OR blocked")).all()
    for r in rows:
        publish(f"traffic/{r[0]}/status", {"road_id": r[0], "congestion_level": r[1], "blocked": r[2],
                                          "incident_multiplier": r[3], "speed_limit_kph": r[4],
                                          "current_speed_kph": r[5], "source": "BACKEND", "event_type": "SNAPSHOT"},
                retain=True)
    log_event(log, "TRAFFIC_SNAPSHOT_PUBLISHED", roads=len(rows))


async def _loop(name: str, interval: float, fn) -> None:
    while True:
        try:
            await asyncio.to_thread(fn)
        except Exception:
            log.exception("background loop %s failed", name)
        await asyncio.sleep(interval)


def _update_gauges() -> None:
    with get_engine().connect() as conn:
        amb = dict(conn.execute(text("SELECT status, count(*) FROM ambulances GROUP BY 1")).all())
        metrics.AVAILABLE_AMBULANCES.set(amb.get("AVAILABLE", 0))
        metrics.ACTIVE_AMBULANCES.set(sum(v for k, v in amb.items() if k not in ("AVAILABLE", "OFFLINE", "MAINTENANCE")))
        metrics.ACTIVE_INCIDENTS.set(conn.execute(text(
            "SELECT count(*) FROM emergency_incidents WHERE status NOT IN ('COMPLETED','CANCELLED')")).scalar())
        k = get_settings().sim_time_scale
        r = conn.execute(text("SELECT avg(extract(epoch FROM arrived_at-created_at))*:k, avg(extract(epoch FROM dispatched_at-created_at))*:k "
                              "FROM emergency_incidents WHERE source <> 'HISTORICAL_SEED'"), {"k": k}).first()
        metrics.AVERAGE_RESPONSE.set(r[0] or 0)
        metrics.AVERAGE_DISPATCH.set(r[1] or 0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    run_migrations()
    await asyncio.to_thread(init_runtime)
    STATE.ws.bind(asyncio.get_running_loop())
    tasks = [asyncio.create_task(STATE.ws.sender())]
    if s.mqtt_enabled:
        from app.mqtt.client import MqttBridge
        from app.mqtt.handlers import on_message
        STATE.mqtt = MqttBridge(s.mqtt_host, s.mqtt_port, on_message, publish_traffic_snapshot)
        STATE.mqtt.start()
    if s.background_tasks:
        from app.services.dispatch_service import dispatcher_cycle
        from app.services.mission_service import mission_tick
        from app.services.routes_service import check_routes
        from app.services.telemetry_service import TELEMETRY
        tasks += [
            asyncio.create_task(_loop("telemetry-flush", 1.0, lambda: TELEMETRY.flush(get_engine()))),
            asyncio.create_task(_loop("missions", 1.0, mission_tick)),
            asyncio.create_task(_loop("dispatcher", 3.0, dispatcher_cycle)),
            asyncio.create_task(_loop("route-monitor", 5.0, check_routes)),
            asyncio.create_task(_loop("gauges", 10.0, _update_gauges)),
        ]
    log_event(log, "BACKEND_STARTED", city=s.city_name, mqtt=s.mqtt_enabled, osrm=s.osrm_url or None)
    yield
    for t in tasks:
        t.cancel()
    if STATE.mqtt is not None:
        STATE.mqtt.stop()


app = FastAPI(
    title="AI-Powered Emergency Vehicle Dispatch & Traffic-Aware Routing",
    version="1.0.0",
    description="Academic project. Severity model trained on SYNTHETIC data - not for clinical use. "
                "Traffic and GPS are simulated.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=[get_settings().frontend_url, "http://localhost:5173",
                                                  "http://127.0.0.1:5173", "http://localhost:4173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
for r in (health.router, auth.router, emergencies.router, fleet.router, routes.router, traffic.router,
          analytics.router, ml.router, simulation.router):
    app.include_router(r)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws")
async def websocket(ws: WebSocket, token: str | None = None):
    """Real-time event stream. Connect with /ws?token=<JWT>."""
    try:
        decode_token(token or "")
    except Exception:
        await ws.close(code=4401)
        return
    await STATE.ws.connect(ws)
    try:
        await ws.send_json({"type": "CONNECTED", "data": {"clients": len(STATE.ws.connections)}})
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"type":"PONG","data":{}}')
    except WebSocketDisconnect:
        pass
    finally:
        STATE.ws.disconnect(ws)
