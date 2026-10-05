"""REST API tests (auth, RBAC, validation, emergencies, dispatch, traffic, analytics)."""
from app.services.state import STATE
from tests.conftest import CRITICAL_CASE, MILD_CASE

CENTER = (12.9716, 77.5946)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"]["ok"] and body["ml_model"]["available"]
    assert body["routing"]["mode"] == "synthetic-fallback"


def test_login_and_rbac(client, viewer_headers, dispatcher_headers):
    assert client.post("/api/auth/login", json={"username": "viewer", "password": "wrong"}).status_code == 401
    assert client.get("/api/emergencies").status_code == 401
    assert client.get("/api/auth/me", headers=viewer_headers).json()["role"] == "VIEWER"
    payload = {**MILD_CASE, "latitude": CENTER[0], "longitude": CENTER[1]}
    assert client.post("/api/emergencies", json=payload, headers=viewer_headers).status_code == 403
    assert client.get("/api/users", headers=dispatcher_headers).status_code == 403


def test_input_validation(client, dispatcher_headers):
    bad = {**MILD_CASE, "latitude": 123.0, "longitude": CENTER[1]}
    assert client.post("/api/emergencies", json=bad, headers=dispatcher_headers).status_code == 422
    bad = {**MILD_CASE, "heart_rate": -5, "latitude": CENTER[0], "longitude": CENTER[1]}
    assert client.post("/api/emergencies", json=bad, headers=dispatcher_headers).status_code == 422
    # valid coordinates but outside the PostGIS service area polygon (ST_Contains)
    far = {**MILD_CASE, "latitude": 13.5, "longitude": 77.5946, "auto_dispatch": False}
    r = client.post("/api/emergencies", json=far, headers=dispatcher_headers)
    assert r.status_code == 422 and "outside the service area" in r.json()["detail"]


def test_create_retrieve_and_dispatch(client, dispatcher_headers, viewer_headers):
    payload = {**CRITICAL_CASE, "latitude": CENTER[0] + 0.004, "longitude": CENTER[1] - 0.003, "auto_dispatch": False}
    r = client.post("/api/emergencies", json=payload, headers=dispatcher_headers)
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["status"] == "WAITING"
    assert inc["predicted_severity"] == "CRITICAL" and inc["ml_status"] == "OK"
    assert inc["rule_severity"] == "CRITICAL" and inc["required_capability"] == "ICU"
    assert 0 < inc["priority"] <= 100 and set(inc["priority_components"]) >= {"severity", "waiting", "distance", "resource"}
    got = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert got["reference"] == inc["reference"] and got["ml_prediction"]["predicted_class"] == "CRITICAL"
    cands = client.get(f"/api/emergencies/{inc['id']}/candidates", headers=viewer_headers).json()
    assert len(cands["candidates"]) >= 2
    r = client.post(f"/api/emergencies/{inc['id']}/dispatch", headers=dispatcher_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "DISPATCHED"
    disp = d["dispatch"]
    assert disp["ambulance_id"] == cands["recommended"]
    scores = [c["score"] for c in disp["candidates"] if c["suitable"]]
    assert round(disp["score"], 4) == min(scores)
    assert "Why was" in disp["explanation"]
    route = d["routes"][0]
    assert route["leg"] == "TO_PATIENT" and route["distance_m"] > 0 and route["adjusted_duration_s"] >= route["base_duration_s"]
    amb = client.get(f"/api/ambulances/{disp['ambulance_id']}", headers=viewer_headers).json()
    assert amb["status"] == "DISPATCHED" and amb["current_incident"] == inc["id"]
    # dispatching again is rejected
    assert client.post(f"/api/emergencies/{inc['id']}/dispatch", headers=dispatcher_headers).status_code == 409
    # clean up: cancel frees the ambulance
    assert client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers).json()["status"] == "CANCELLED"
    assert client.get(f"/api/ambulances/{disp['ambulance_id']}", headers=viewer_headers).json()["status"] == "AVAILABLE"


def test_no_available_ambulance_returns_clear_error(client, dispatcher_headers, admin_headers):
    ambs = client.get("/api/ambulances", headers=admin_headers).json()
    for a in ambs:
        client.patch(f"/api/ambulances/{a['id']}", json={"status": "OFFLINE"}, headers=admin_headers)
    try:
        payload = {**MILD_CASE, "latitude": CENTER[0], "longitude": CENTER[1], "auto_dispatch": False}
        inc = client.post("/api/emergencies", json=payload, headers=dispatcher_headers).json()
        r = client.post(f"/api/emergencies/{inc['id']}/dispatch", headers=dispatcher_headers)
        assert r.status_code == 409 and "No available ambulance" in r.json()["detail"]
        client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
    finally:
        for a in ambs:
            client.patch(f"/api/ambulances/{a['id']}", json={"status": "AVAILABLE"}, headers=admin_headers)


def test_traffic_event_changes_state_and_route_cost(client, dispatcher_headers, viewer_headers):
    o = {"latitude": CENTER[0] - 0.008, "longitude": CENTER[1] - 0.008}
    d = {"latitude": CENTER[0] + 0.008, "longitude": CENTER[1] + 0.008}
    before = client.post("/api/routes/calculate", json={"origin": o, "destination": d}, headers=viewer_headers).json()
    road = max(before["segments"], key=lambda s: s["length_m"])["road_id"]
    r = client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": road}, headers=dispatcher_headers)
    assert r.status_code == 201 and r.json()["changes"][0]["blocked"] is True
    assert STATE.graph.road_state(road).blocked
    after = client.post("/api/routes/calculate", json={"origin": o, "destination": d}, headers=viewer_headers).json()
    assert road not in {s["road_id"] for s in after["segments"]}
    assert after["adjusted_duration_s"] >= before["adjusted_duration_s"]
    events = client.get("/api/traffic/events", headers=viewer_headers).json()
    assert events[0]["road_id"] == road and events[0]["event_type"] == "BLOCK"
    r = client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": road}, headers=dispatcher_headers)
    assert r.json()["changes"][0]["congestion_level"] == "FREE"
    # point-based accident uses PostGIS ST_DWithin to find nearby roads
    r = client.post("/api/traffic/events", json={"event_type": "ACCIDENT", "latitude": o["latitude"],
                                                 "longitude": o["longitude"], "radius_m": 400}, headers=dispatcher_headers)
    assert r.status_code == 201 and r.json()["changes"][0]["congestion_level"] == "SEVERE"
    assert client.post("/api/traffic/reset", headers=dispatcher_headers).json()["cleared_roads"] >= 1
    assert client.post("/api/traffic/events", json={"event_type": "BLOCK", "road_id": "NOPE"},
                       headers=dispatcher_headers).status_code == 422


def test_analytics_endpoints(client, viewer_headers):
    s = client.get("/api/analytics/summary", headers=viewer_headers).json()
    assert s["historical_seed"]["incidents"] == 10
    assert s["hospitals"] == 4 and "fleet_utilization_pct" in s
    rt = client.get("/api/analytics/response-times", headers=viewer_headers).json()
    assert len(rt["items"]) >= 10 and all(i["response_s"] > 0 for i in rt["items"])
    b = client.get("/api/analytics/breakdowns", headers=viewer_headers).json()
    assert sum(x["count"] for x in b["by_type"]) >= 10


def test_ml_predict_endpoint(client, viewer_headers):
    r = client.post("/api/ml/predict", json=CRITICAL_CASE, headers=viewer_headers).json()
    assert r["ml"]["severity"] == "CRITICAL" and r["rule"]["severity"] == "CRITICAL"
    assert client.get("/api/ml/model", headers=viewer_headers).json()["available"]


def test_simulation_schedule_is_deterministic(client, viewer_headers):
    a = client.get("/api/simulation/schedule-preview?seed=42&incidents=4&traffic_events=4", headers=viewer_headers).json()
    b = client.get("/api/simulation/schedule-preview?seed=42&incidents=4&traffic_events=4", headers=viewer_headers).json()
    c = client.get("/api/simulation/schedule-preview?seed=43&incidents=4&traffic_events=4", headers=viewer_headers).json()
    assert a == b and a != c


def test_websocket_requires_token_and_streams(client, dispatcher_headers):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    token = dispatcher_headers["Authorization"].split()[1]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "CONNECTED"
        road = STATE.graph.road_ids[0]
        r = client.post("/api/traffic/events", json={"event_type": "CONGESTION", "road_id": road, "level": "HEAVY"},
                        headers=dispatcher_headers)
        assert r.status_code == 201
        for _ in range(20):                       # other broadcasts may arrive first
            msg = ws.receive_json()
            if msg["type"] == "TRAFFIC_CHANGED" and msg["data"]["road_id"] == road:
                break
        assert msg["data"]["congestion_level"] == "HEAVY"
        client.post("/api/traffic/events", json={"event_type": "CLEAR", "road_id": road}, headers=dispatcher_headers)


def test_zero_length_route_is_stored(client, viewer_headers):
    """Regression: an incident located exactly on a hospital node produced an empty LINESTRING."""
    from app.services.state import STATE
    lat, lon = float(STATE.graph.lat[0]), float(STATE.graph.lon[0])
    p = {"latitude": lat, "longitude": lon}
    r = client.post("/api/routes/calculate", json={"origin": p, "destination": p}, headers=viewer_headers)
    assert r.status_code == 200, r.text
    assert r.json()["distance_m"] == 0 and len(r.json()["geometry"]) == 2
