"""Deterministic end-to-end test of the unified decision pipeline (synthetic test network, no broker/OSRM):

Emergency -> ML prediction -> confidence assessment -> dispatch decision (+explanation) -> ambulance selection ->
route calculation with traffic prediction -> MQTT FOLLOW_ROUTE -> telemetry -> traffic change -> automatic reroute
(MQTT ROUTE_UPDATED) -> reroute cooldown -> scene arrival -> hospital congestion prediction -> hospital selection ->
hospital route -> hospital arrival -> completion -> decision trace.
"""
import time

import pytest

from app.config import get_settings
from app.database import session_scope
from app.services.mission_service import handle_sim_status, mission_tick
from app.services.routes_service import ACTIVE, check_routes, position_on_route
from app.services.state import STATE
from app.services.telemetry_service import TELEMETRY
from app.services.traffic_prediction import PREDICTOR
from tests.conftest import CRITICAL_CASE


class RecordingBridge:
    connected = True

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False, qos=1):
        self.sent.append((topic, payload, retain))
        return True

    def commands(self, amb):
        return [p for t, p, _ in self.sent if t == f"ambulance/{amb}/command"]


@pytest.fixture
def bridge():
    old = STATE.mqtt
    STATE.mqtt = RecordingBridge()
    yield STATE.mqtt
    STATE.mqtt = old


def sim(amb, event, route_id):
    with STATE.lock, session_scope() as db:
        handle_sim_status(db, amb, {"event": event, "route_id": route_id})


def wait_for(fn, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return None


def test_full_decision_pipeline(client, dispatcher_headers, viewer_headers, bridge):
    PREDICTOR.predict()                                         # traffic prediction cycle (fallback model in tests)
    p = {**CRITICAL_CASE, "latitude": 12.9716 + 0.006, "longitude": 77.5946 - 0.007, "auto_dispatch": True}
    inc = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
    # ML + confidence + dispatch decision
    assert inc["predicted_severity"] == "CRITICAL" and inc["confidence_level"] == "HIGH"
    assert inc["decision_mode"] == "AUTO_DISPATCH" and inc["status"] == "DISPATCHED"
    amb = inc["assigned_ambulance"]
    ar = ACTIVE.get(amb)
    dec = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()
    assert dec["dispatch"]["explanation"]["selected"] == amb and dec["dispatch"]["explanation"]["reasons"]
    r0 = dec["routes"][0]
    assert r0["predicted_duration_s"] is not None and r0["prediction_horizon_min"] == get_settings().traffic_prediction_horizon_min
    # MQTT dispatch
    cmd = bridge.commands(amb)[-1]
    assert cmd["command"] == "FOLLOW_ROUTE" and cmd["route_id"] == str(ar.route_id) and cmd["geometry"]
    assert any(t == f"emergency/{inc['id']}/created" for t, _, _ in bridge.sent)

    # telemetry
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    _, _, (lat, lon) = position_on_route(ar, ar.total_m * 0.1)
    TELEMETRY.handle_location(amb, {"latitude": lat, "longitude": lon, "speed": 40, "route_id": str(ar.route_id),
                                    "progress_m": ar.total_m * 0.1})
    assert TELEMETRY.latest[amb]["eta_remaining_s"] > 0

    # traffic change on the route ahead -> automatic reroute with ROUTE_UPDATED over MQTT
    rem, _ = STATE.router.remaining(ar.segments, ar.progress_m)
    ahead = [s.road_id for s in rem[2:] if s.road_id]
    blocked = ahead[len(ahead) // 2]
    assert client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": blocked},
                       headers=dispatcher_headers).status_code == 201
    # while the active route still crosses the blocked road its live ETA is infinite; the API must stay valid JSON
    assert client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).status_code == 200
    assert client.get(f"/api/routes/{ar.route_id}", headers=viewer_headers).status_code == 200
    new = wait_for(lambda: ACTIVE.get(amb) if ACTIVE.get(amb).route_id != ar.route_id else None)
    assert new is not None, "no automatic reroute"
    upd = bridge.commands(amb)[-1]
    assert upd["command"] == "ROUTE_UPDATED" and upd["replaces_route_id"] == str(ar.route_id)
    assert upd["route_id"] == str(new.route_id) and blocked not in {s["road_id"] for s in upd["segments"]}

    # cooldown: a second disruption right after the reroute must not cause another (oscillating) reroute
    st = get_settings()
    nrem, _ = STATE.router.remaining(new.segments, new.progress_m)
    nahead = [s.road_id for s in nrem[2:] if s.road_id and s.road_id != blocked]
    client.post("/api/traffic/events", json={"event_type": "CONGESTION", "road_id": nahead[len(nahead) // 2],
                                             "level": "SEVERE"}, headers=dispatcher_headers)
    time.sleep(1.0)
    check_routes()
    assert ACTIVE.get(amb).route_id == new.route_id
    assert new.last_reroute_at is not None and st.reroute_cooldown_s > 0
    client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": nahead[len(nahead) // 2]}, headers=dispatcher_headers)
    client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": blocked}, headers=dispatcher_headers)

    # scene arrival -> hospital prediction -> hospital selection -> hospital route (MQTT)
    sim(amb, "ARRIVED", str(ACTIVE.get(amb).route_id))
    time.sleep(1.1)
    mission_tick()
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "TO_HOSPITAL"
    hc = d["dispatch"]["hospital_candidates"]
    assert all(h["forecast"]["estimated"] for h in hc) and hc[0]["hospital_id"] == d["destination_hospital"]
    assert {"predicted_load_pct", "expected_wait_min", "method", "model_version"} <= set(hc[0]["forecast"])
    leg = ACTIVE.get(amb)
    assert leg.leg == "TO_HOSPITAL" and bridge.commands(amb)[-1]["command"] == "FOLLOW_ROUTE"

    # hospital arrival -> completion
    sim(amb, "ARRIVED", str(leg.route_id))
    time.sleep(1.1)
    mission_tick()
    assert client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()["status"] == "COMPLETED"
    assert bridge.commands(amb)[-1]["command"] == "IDLE"

    # decision trace generated from stored events, in order
    trace = [t["event"] for t in client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()["trace"]]
    order = ["EMERGENCY_CREATED", "EMERGENCY_CLASSIFIED", "CONFIDENCE_ASSESSED", "DISPATCH_DECISION", "ROUTE_SELECTED",
             "ROUTE_DEGRADATION_DETECTED", "ROUTE_RECALCULATED", "HOSPITAL_CONGESTION_PREDICTED", "HOSPITAL_SELECTED",
             "INCIDENT_COMPLETED"]
    pos = [trace.index(e) for e in order]
    assert pos == sorted(pos), trace
