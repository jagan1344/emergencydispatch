"""End-to-end backend workflow without external services: the MQTT simulator is replaced by direct calls
to the same handlers the MQTT bridge uses.

Emergency → severity → candidate selection → route → dispatch → movement → traffic block → re-route →
arrival → patient loaded → hospital selection → arrival at hospital → completion → analytics.
"""
import time
import uuid

from sqlalchemy import select, text

from app.database import session_scope
from app.models import Route, SystemEvent
from app.services.mission_service import handle_sim_status, mission_tick
from app.services.routes_service import ACTIVE, check_routes, position_on_route
from app.services.state import STATE
from app.services.telemetry_service import TELEMETRY
from tests.conftest import CRITICAL_CASE


def sim_status(amb_id, event, route_id):
    with STATE.lock, session_scope() as db:
        handle_sim_status(db, amb_id, {"event": event, "route_id": route_id})


def sim_location(amb_id, ar, progress):
    _, _, (lat, lon) = position_on_route(ar, progress)
    TELEMETRY.handle_location(amb_id, {"latitude": lat, "longitude": lon, "speed": 40, "route_id": str(ar.route_id),
                                       "progress_m": progress})


def test_full_workflow(client, dispatcher_headers, viewer_headers):
    payload = {**CRITICAL_CASE, "latitude": 12.9716 - 0.006, "longitude": 77.5946 + 0.007, "auto_dispatch": True}
    inc = client.post("/api/emergencies", json=payload, headers=dispatcher_headers).json()
    assert inc["status"] == "DISPATCHED", inc.get("dispatch_error")
    assert inc["severity"] == "CRITICAL"
    amb = inc["assigned_ambulance"]
    ar = ACTIVE.get(amb)
    assert ar is not None and ar.leg == "TO_PATIENT"

    sim_status(amb, "ROUTE_STARTED", str(ar.route_id))
    assert client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()["status"] == "EN_ROUTE"
    sim_location(amb, ar, ar.total_m * 0.1)
    assert TELEMETRY.latest[amb]["eta_remaining_s"] > 0

    # block a road ahead of the ambulance -> automatic re-route
    rem, _ = STATE.router.remaining(ar.segments, ar.progress_m)
    ahead = [s.road_id for s in rem[2:] if s.road_id]
    assert ahead, "route too short for the test"
    blocked = ahead[len(ahead) // 2]
    r = client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": blocked}, headers=dispatcher_headers)
    assert r.status_code == 201
    deadline = time.time() + 10
    new = None
    while time.time() < deadline:
        cur = ACTIVE.get(amb)
        if cur is not None and cur.route_id != ar.route_id:
            new = cur
            break
        check_routes()
        time.sleep(0.2)
    assert new is not None, "route was not recalculated after blocking a road on it"
    assert blocked not in {s.road_id for s in new.segments}
    with session_scope() as db:
        rr = db.scalar(select(Route).where(Route.id == new.route_id))
        assert rr.reroute_of == ar.route_id and "blocked" in rr.reroute_reason
        assert db.scalar(select(SystemEvent).where(SystemEvent.event_type == "ROUTE_RECALCULATED",
                                                   SystemEvent.incident_id == uuid.UUID(inc["id"]))) is not None
    client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": blocked}, headers=dispatcher_headers)

    # arrival at scene
    sim_location(amb, new, new.total_m)
    sim_status(amb, "ARRIVED", str(new.route_id))
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "ARRIVED" and d["response_time_s"] > 0
    time.sleep(1.1)                                   # scene time: 60 sim-s at x60 = 1 s
    mission_tick()
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "TO_HOSPITAL" and d["destination_hospital"]
    assert d["dispatch"]["hospital_explanation"] and len(d["dispatch"]["hospital_candidates"]) >= 2
    hosp_leg = ACTIVE.get(amb)
    assert hosp_leg.leg == "TO_HOSPITAL"

    sim_status(amb, "ARRIVED", str(hosp_leg.route_id))
    time.sleep(1.1)
    mission_tick()
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "COMPLETED"
    assert client.get(f"/api/ambulances/{amb}", headers=viewer_headers).json()["status"] == "AVAILABLE"
    types = [e["type"] for e in d["timeline"]]
    for t in ("EMERGENCY_CREATED", "EMERGENCY_CLASSIFIED", "DISPATCH_CREATED", "ROUTE_RECALCULATED",
              "HOSPITAL_SELECTED", "INCIDENT_COMPLETED"):
        assert t in types, t

    TELEMETRY.flush(STATE.graph and __import__("app.database", fromlist=["get_engine"]).get_engine())
    s = client.get("/api/analytics/summary", headers=viewer_headers).json()
    assert s["completed_live"] >= 1 and s["reroutes"] >= 1 and s["avg_response_time_s"] > 0
    with session_scope() as db:
        assert db.execute(text("SELECT count(*) FROM ambulance_locations WHERE ambulance_id=:a"), {"a": amb}).scalar() >= 2


def test_priority_queue_with_ortools_batch(client, dispatcher_headers, viewer_headers):
    """Two waiting incidents dispatched in one cycle via the OR-Tools assignment."""
    from app.services.dispatch_service import dispatcher_cycle
    ids = []
    for k, case in enumerate((CRITICAL_CASE, {**CRITICAL_CASE, "consciousness": "ALERT", "bleeding": "MINOR"})):
        p = {**case, "latitude": 12.9716 + 0.003 * k, "longitude": 77.5946 - 0.004, "auto_dispatch": False}
        ids.append(client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()["id"])
    done = dispatcher_cycle()
    assert set(ids) <= set(done)
    methods = {client.get(f"/api/emergencies/{i}", headers=viewer_headers).json()["dispatch"]["method"] for i in ids}
    assert methods == {"ORTOOLS_ASSIGNMENT"}
    ambs = {client.get(f"/api/emergencies/{i}", headers=viewer_headers).json()["assigned_ambulance"] for i in ids}
    assert len(ambs) == 2
    for i in ids:
        client.post(f"/api/emergencies/{i}/cancel", headers=dispatcher_headers)
