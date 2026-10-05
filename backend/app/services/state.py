"""Process-wide runtime state (graph, routing engine, ML model, connections)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.ml.predict import SeverityModel
from app.routing.engine import RoutingEngine
from app.routing.graph import RoadGraph
from app.routing.osrm_client import OsrmClient
from app.websocket.manager import ConnectionManager


@dataclass
class AppState:
    ws: ConnectionManager = field(default_factory=ConnectionManager)
    model: SeverityModel = field(default_factory=SeverityModel)
    osrm: OsrmClient | None = None
    graph: RoadGraph | None = None
    router: RoutingEngine | None = None
    graph_error: str | None = None
    mqtt: object | None = None                 # app.mqtt.client.MqttBridge
    # serialises every state-machine mutation (dispatch / mission transitions / re-routing)
    lock: threading.RLock = field(default_factory=threading.RLock)
    simulator_last_seen: float | None = None
    traffic_sim_last_seen: float | None = None


STATE = AppState()


def require_router() -> RoutingEngine:
    if STATE.router is None:
        raise RuntimeError(STATE.graph_error or "routing engine not initialised")
    return STATE.router
