"""Restart recovery: route progress / ETA persisted by the telemetry flush, restored after a backend restart, and a
restarted simulator resumes from the last persisted position (no teleport, no new route)."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulator"))

import ambulance_simulator  # noqa: E402
from ambulance_simulator import AmbulanceSimulator  # noqa: E402
from common import haversine_m  # noqa: E402

from app.database import get_engine, session_scope  # noqa: E402
from app.services.routes_service import ACTIVE, load_active_routes, position_on_route, resume_simulated_routes  # noqa: E402
from app.services.telemetry_service import TELEMETRY  # noqa: E402
from tests.conftest import MILD_CASE  # noqa: E402
from tests.test_e2e_decision import RecordingBridge, bridge, sim  # noqa: E402,F401


def seg(a, b, road):
    return {"road_id": road, "length_m": haversine_m(*a, *b), "speed_kph": 36.0, "base_speed_kph": 36.0,
            "coords": [list(a), list(b)]}


A, B, C = (0.0, 0.0), (0.0, 0.009), (0.0, 0.018)             # two ~1 km segments
ROUTE = {"command": "FOLLOW_ROUTE", "route_id": "r1", "leg": "TO_PATIENT", "time_scale": 10,
         "segments": [seg(A, B, "R1"), seg(B, C, "R2")]}


# ------------------------------------------------------------------------------------------ simulator side
def test_restarted_simulator_holds_replayed_route_and_resumes_from_persisted_position():
    sim_ = AmbulanceSimulator("localhost", 1883)
    sim_._command("AMB-R", dict(ROUTE, resume={"latitude": 0.0, "longitude": 0.002, "progress_m": 222.0}), retained=True)
    u = sim_.units["AMB-R"]
    assert u.mission is None and u.pending is not None             # replayed command (and its stale resume) held
    assert sim_.step_unit(u, 1.0) == []                             # does not drive from the route start
    gps = (0.0, 0.0135)                                             # last persisted fix: 1.5 km along the route
    sim_._command("AMB-R", dict(ROUTE, resume={"latitude": gps[0], "longitude": gps[1], "progress_m": 1500.0}))
    m = u.mission
    assert m is not None and m.route_id == "r1" and u.pending is None
    assert m.seg_idx == 1 and m.progress_m == pytest.approx(1500.0) and m.started
    assert haversine_m(u.lat, u.lon, *gps) < 5                      # continues where the ambulance is
    msgs = sim_.step_unit(u, 1.0)
    assert not any(p.get("event") == "ROUTE_STARTED" for _, p, _ in msgs)
    loc = next(p for t, p, _ in msgs if t.endswith("/location"))
    assert haversine_m(loc["latitude"], loc["longitude"], *gps) < 120 and loc["progress_m"] > 1500
    sim_._command("AMB-R", dict(ROUTE, resume={"latitude": 0.0, "longitude": 0.0, "progress_m": 0.0}))
    assert u.mission is m and m.progress_m > 1500                   # duplicate route id: no reset


def test_resume_point_inconsistent_with_gps_is_not_used():
    sim_ = AmbulanceSimulator("localhost", 1883)
    sim_._command("AMB-X", dict(ROUTE, resume={"latitude": 0.02, "longitude": 0.02, "progress_m": 100.0}))
    u = sim_.units["AMB-X"]
    assert u.mission is None and u.pending is not None and u.lat is None   # no teleport to a guessed position


def test_without_resume_answer_the_held_route_starts_as_before(monkeypatch):
    monkeypatch.setattr(ambulance_simulator, "RESUME_WAIT_S", 0.0)
    sim_ = AmbulanceSimulator("localhost", 1883)
    sim_._command("AMB-O", ROUTE, retained=True)
    u = sim_.units["AMB-O"]
    with sim_.lock:
        sim_.release_stale_pending()
    assert u.mission is not None and (u.lat, u.lon) == A and u.pending is None


# ------------------------------------------------------------------------------------------ backend side
def _dispatched(client, dispatcher_headers):
    client.post("/api/simulation/reset", json={}, headers=dispatcher_headers)
    p = {**MILD_CASE, "latitude": 12.9716 - 0.004, "longitude": 77.5946 + 0.005, "auto_dispatch": True}
    inc = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
    assert inc["status"] == "DISPATCHED", inc
    return inc, ACTIVE.get(inc["assigned_ambulance"])


def test_progress_is_persisted_restored_and_resumed(client, dispatcher_headers, bridge):
    inc, ar = _dispatched(client, dispatcher_headers)
    amb = inc["assigned_ambulance"]
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    progress = ar.total_m * 0.4
    _, _, (lat, lon) = position_on_route(ar, progress)
    TELEMETRY.handle_location(amb, {"latitude": lat, "longitude": lon, "speed": 40, "route_id": str(ar.route_id),
                                    "progress_m": progress})
    TELEMETRY.flush(get_engine())
    with get_engine().connect() as c:
        row = c.execute(text("SELECT progress_m, last_eta_s, progress_updated_at FROM routes WHERE id=:id"),
                        {"id": ar.route_id}).one()
        fix = c.execute(text("SELECT progress_m FROM ambulance_locations WHERE route_id=:id ORDER BY recorded_at DESC "
                             "LIMIT 1"), {"id": ar.route_id}).scalar()
    assert row[0] == pytest.approx(progress, abs=0.1) and row[1] > 0 and row[2] is not None
    assert fix == pytest.approx(progress, abs=0.1)

    # backend restart: the in-memory cache is rebuilt from the database with the persisted progress
    ACTIVE.clear()
    TELEMETRY.latest.clear()
    with session_scope() as db:
        load_active_routes(db)
    restored = ACTIVE.get(amb)
    assert restored.route_id == ar.route_id and restored.progress_m == pytest.approx(progress, abs=0.1)

    # simulator restart: the same route is re-sent with the persisted position (no new route)
    n_routes = _active_routes(amb)
    bridge.sent.clear()
    out = resume_simulated_routes()
    mine = next(o for o in out if o["ambulance_id"] == amb)
    assert mine["decision"] == "RESUME" and mine["offset_m"] < 5
    cmd = bridge.commands(amb)[-1]
    assert cmd["command"] == "FOLLOW_ROUTE" and cmd["route_id"] == str(ar.route_id)
    assert cmd["resume"]["progress_m"] == pytest.approx(progress, abs=0.1)
    assert haversine_m(cmd["resume"]["latitude"], cmd["resume"]["longitude"], lat, lon) < 1
    assert _active_routes(amb) == n_routes == 1

    # a real simulator receiving that command continues at the persisted point
    sim_ = AmbulanceSimulator("localhost", 1883)
    sim_._command(amb, cmd)
    u = sim_.units[amb]
    assert haversine_m(u.lat, u.lon, lat, lon) < 5 and u.mission.progress_m == pytest.approx(progress, abs=0.5)


def test_resume_replans_from_gps_when_progress_disagrees(client, dispatcher_headers, bridge):
    inc, ar = _dispatched(client, dispatcher_headers)
    amb = inc["assigned_ambulance"]
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    _, _, far = position_on_route(ar, ar.total_m * 0.7)
    TELEMETRY.handle_location(amb, {"latitude": far[0], "longitude": far[1], "speed": 40,
                                    "route_id": str(ar.route_id), "progress_m": 0.0})   # GPS far from progress 0
    bridge.sent.clear()
    out = next(o for o in resume_simulated_routes() if o["ambulance_id"] == amb)
    assert out["decision"] == "REPLANNED_FROM_GPS" and out["offset_m"] > 75
    cmd = bridge.commands(amb)[-1]
    assert cmd["command"] == "ROUTE_UPDATED" and cmd["replaces_route_id"] == str(ar.route_id)
    assert haversine_m(*cmd["segments"][0]["coords"][0], *far) < 5       # new route starts at the GPS fix
    assert _active_routes(amb) == 1                                       # replaced, not duplicated


def _active_routes(amb: str) -> int:
    with get_engine().connect() as c:
        return c.execute(text("SELECT count(*) FROM routes WHERE ambulance_id=:a AND active"), {"a": amb}).scalar()


# ------------------------------------------------------------------------------------------ no drivable route
def test_route_unavailable_is_explicit_and_not_repeated(client, dispatcher_headers, viewer_headers, bridge):
    from app.services.routes_service import check_routes
    from app.services.state import STATE
    from tests.test_e2e_decision import wait_for
    inc, ar = _dispatched(client, dispatcher_headers)
    amb = inc["assigned_ambulance"]
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    g = STATE.graph
    node, _ = g.nearest_node(inc["latitude"], inc["longitude"])
    access = sorted({g.road_ids[int(g.e_road[e])] for e in range(len(g.e_from)) if int(g.e_to[e]) == node})
    for road in access:
        assert client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": road},
                           headers=dispatcher_headers).status_code == 201
    try:
        def events():
            with get_engine().connect() as c:
                return c.execute(text("SELECT payload FROM system_events WHERE event_type='ROUTE_UNAVAILABLE' "
                                      "AND incident_id=:i"), {"i": inc["id"]}).scalars().all()
        assert wait_for(events), "no ROUTE_UNAVAILABLE event"
        def degradations():
            with get_engine().connect() as c:
                return c.execute(text("SELECT count(*) FROM system_events WHERE event_type='ROUTE_DEGRADATION_DETECTED' "
                                      "AND incident_id=:i"), {"i": inc["id"]}).scalar()
        n_deg = degradations()
        check_routes()                                              # monitor runs again: no repeated alert
        check_routes()
        ev = events()
        assert len(ev) == 1 and degradations() == n_deg
        assert ev[0]["dispatcher_required"] is True and ev[0]["alternative_exists"] is False
        assert set(ev[0]["blocked_roads"]) & set(access) and ev[0]["ambulance_id"] == amb
        # (blocking the first access road may legitimately re-route via the other one first)
        assert str(ACTIVE.get(amb).route_id) == ev[0]["old_route_id"] and _active_routes(amb) == 1   # kept, not walked
        # a new call at the same place: explicit dispatcher review instead of a walking-speed route
        p = {**MILD_CASE, "latitude": inc["latitude"], "longitude": inc["longitude"], "auto_dispatch": True}
        inc2 = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
        assert inc2["status"] == "WAITING"
        assert "ROUTE UNAVAILABLE" in (inc2.get("dispatch_note") or inc2.get("dispatch_error") or "")
    finally:
        for road in access:
            client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": road}, headers=dispatcher_headers)


# ------------------------------------------------------------------------------------------ backend restart (checkpoint)
def _restart_backend_state():
    """What a backend restart loses (in-memory state) and rebuilds from the database."""
    from app.services.routes_service import recover_active_routes
    ACTIVE.clear()
    TELEMETRY.latest.clear()
    with session_scope() as db:
        load_active_routes(db)
    return recover_active_routes()


def _events(incident_id, kind):
    with get_engine().connect() as c:
        return c.execute(text("SELECT payload FROM system_events WHERE event_type=:k AND incident_id=:i ORDER BY id"),
                         {"k": kind, "i": incident_id}).scalars().all()


def test_backend_restart_recovers_route_from_checkpoint(client, dispatcher_headers, viewer_headers, bridge):
    from app.services.routes_service import remaining_eta
    inc, ar = _dispatched(client, dispatcher_headers)
    amb = inc["assigned_ambulance"]
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    progress = ar.total_m * 0.4
    _, _, (lat, lon) = position_on_route(ar, progress)
    TELEMETRY.flush(get_engine())                     # drain positions left over by earlier tests
    TELEMETRY.handle_location(amb, {"latitude": lat, "longitude": lon, "speed": 40, "route_id": str(ar.route_id),
                                    "progress_m": progress})
    assert TELEMETRY.flush(get_engine())["checkpoints"] == 1
    eta_before = remaining_eta(ar)
    full_eta = ar.planned_eta_s
    with get_engine().connect() as c:
        cp = c.execute(text("SELECT checkpoint_lat, checkpoint_lon, checkpoint_segment, progress_m FROM routes "
                            "WHERE id=:id"), {"id": ar.route_id}).one()
    assert abs(cp[0] - lat) < 1e-6 and abs(cp[1] - lon) < 1e-6 and cp[2] is not None and cp[3] == pytest.approx(progress, abs=0.1)

    out = next(o for o in _restart_backend_state() if o["ambulance_id"] == amb)
    restored = ACTIVE.get(amb)
    assert out["decision"] == "ROUTE_RECOVERED" and restored.route_id == ar.route_id
    assert restored.progress_m == pytest.approx(progress, abs=0.1)                   # not reset to the route start
    assert out["distance_m"] < 1 and out["checkpoint"]["progress_m"] == pytest.approx(progress, abs=0.1)
    assert out["new_eta_s"] == pytest.approx(eta_before, rel=0.02) and out["new_eta_s"] < full_eta   # remaining route
    assert _events(inc["id"], "ROUTE_RECOVERED")
    r = client.get(f"/api/routes/{ar.route_id}", headers=viewer_headers).json()
    assert r["status"] == "ACTIVE" and r["checkpoint"]["progress_m"] == pytest.approx(progress, abs=0.1)
    assert r["remaining_m"] == pytest.approx(ar.total_m - progress, abs=1)
    assert _active_routes(amb) == 1


def test_backend_restart_replans_when_gps_left_the_checkpoint(client, dispatcher_headers, bridge):
    inc, ar = _dispatched(client, dispatcher_headers)
    amb = inc["assigned_ambulance"]
    sim(amb, "ROUTE_STARTED", str(ar.route_id))
    _, _, (lat, lon) = position_on_route(ar, ar.total_m * 0.1)
    TELEMETRY.handle_location(amb, {"latitude": lat, "longitude": lon, "speed": 40, "route_id": str(ar.route_id),
                                    "progress_m": ar.total_m * 0.1})
    TELEMETRY.flush(get_engine())
    _, _, far = position_on_route(ar, ar.total_m * 0.8)              # the unit moved on while the backend was down
    with get_engine().begin() as c:
        c.execute(text("UPDATE ambulances SET latitude=:a, longitude=:o WHERE id=:id"), {"a": far[0], "o": far[1], "id": amb})
    bridge.sent.clear()
    out = next(o for o in _restart_backend_state() if o["ambulance_id"] == amb)
    assert out["decision"] == "ROUTE_RECOVERY_REPLAN" and out["distance_m"] > 75 and out["new_route_id"]
    assert out["old_eta_s"] is not None and out["new_eta_s"] is not None
    cmd = bridge.commands(amb)[-1]
    assert cmd["command"] == "ROUTE_UPDATED" and haversine_m(*cmd["segments"][0]["coords"][0], *far) < 5   # from GPS
    assert ACTIVE.get(amb).route_id != ar.route_id and _active_routes(amb) == 1        # replaced, not duplicated
    ev = _events(inc["id"], "ROUTE_RECOVERY_REPLAN")
    assert ev and ev[-1]["reason"].startswith("GPS fix") and ev[-1]["current_position"] and ev[-1]["checkpoint"]


# ------------------------------------------------------------------------------------------ route API: closures
def test_route_api_open_blocked_and_optional_fallback(client, viewer_headers, dispatcher_headers, monkeypatch):
    from app.config import get_settings
    from app.services.state import STATE
    g = STATE.graph
    dest = (12.9716 - 0.004, 77.5946 + 0.005)
    origin = (12.9716 + 0.01, 77.5946 - 0.01)
    body = {"origin": {"latitude": origin[0], "longitude": origin[1]},
            "destination": {"latitude": dest[0], "longitude": dest[1]}}
    ok = client.post("/api/routes/calculate", json=body, headers=viewer_headers)            # 1. normal route
    assert ok.status_code == 200 and ok.json()["status"] in ("ACTIVE", "INACTIVE") and ok.json()["engine"] != "graph-closure"
    node, _ = g.nearest_node(*dest)
    access = sorted({g.road_ids[int(g.e_road[e])] for e in range(len(g.e_from)) if int(g.e_to[e]) == node})
    try:
        for road in access:
            client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": road}, headers=dispatcher_headers)
        bad = client.post("/api/routes/calculate", json=body, headers=viewer_headers)        # 3./4. all blocked
        assert bad.status_code == 409
        d = bad.json()["detail"]
        assert d["status"] == "ROUTE_UNAVAILABLE" and set(d["blocked_roads"]) & set(access)
        assert d["origin"] == [pytest.approx(origin[0]), pytest.approx(origin[1])] and d["destination"]
        assert d["dispatcher_action_required"] is True and "no drivable" in d["reason"]
        assert get_settings().closure_access_fallback is False                              # 5. default off
        monkeypatch.setattr(get_settings(), "closure_access_fallback", True)                # 6. explicit opt-in
        legacy = client.post("/api/routes/calculate", json=body, headers=viewer_headers)
        assert legacy.status_code == 200
        assert any(a["through_closure"] for a in legacy.json()["alternatives"])            # clearly labelled
    finally:
        for road in access:
            client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": road}, headers=dispatcher_headers)
