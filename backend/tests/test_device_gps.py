"""Real-GPS (crew device) path: map-matching, progress, off-route re-routing and arrival detection."""
from app.services.routes_service import ACTIVE, position_on_route
from app.services.state import STATE
from app.utils.geo import destination_point
from tests.conftest import CRITICAL_CASE


def test_device_gps_drives_mission(client, dispatcher_headers, viewer_headers):
    p = {**CRITICAL_CASE, "latitude": 12.9716 + 0.007, "longitude": 77.5946 + 0.006, "auto_dispatch": False,
         "systolic_bp": 88, "temperature_c": 37.9}
    inc = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
    assert inc["systolic_bp"] == 88 and inc["temperature_c"] == 37.9
    d = client.post(f"/api/emergencies/{inc['id']}/dispatch", headers=dispatcher_headers).json()
    amb = d["assigned_ambulance"]
    ar = ACTIVE.get(amb)
    url = f"/api/ambulances/{amb}/gps"

    # 1) first fix on the route -> device mode, mission started, progress map-matched
    _, _, (lat, lon) = position_on_route(ar, ar.total_m * 0.3)
    r = client.post(url, json={"latitude": lat, "longitude": lon, "accuracy_m": 8}, headers=dispatcher_headers).json()
    assert r["off_route_m"] < 15 and abs(r["progress_m"] - ar.total_m * 0.3) < 25
    a = client.get(f"/api/ambulances/{amb}", headers=viewer_headers).json()
    assert a["gps_source"] == "DEVICE" and a["status"] == "EN_ROUTE_TO_PATIENT"

    # 2) two fixes ~400 m away from the route -> new route from the real position
    from app.services.device_gps import project
    cands = [destination_point(lat, lon, b, dist) for b in range(0, 360, 30) for dist in (300, 400, 500)]
    off = max(cands, key=lambda c: project(ar, *c)[1])     # point farthest from every part of the route
    client.post(url, json={"latitude": off[0], "longitude": off[1]}, headers=dispatcher_headers)
    r = client.post(url, json={"latitude": off[0], "longitude": off[1]}, headers=dispatcher_headers).json()
    new = ACTIVE.get(amb)
    assert r["off_route_m"] > 100, r
    assert r.get("rerouted") and new.route_id != ar.route_id
    routes = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()["routes"]
    assert any("left planned route" in (x["reroute_reason"] or "") for x in routes)

    # 3) a fix within 40 m of the patient -> ARRIVED
    r = client.post(url, json={"latitude": new.dest[0], "longitude": new.dest[1]}, headers=dispatcher_headers).json()
    assert r["arrived"]
    assert client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()["status"] == "ARRIVED"

    client.post(f"/api/ambulances/{amb}/gps/release", headers=dispatcher_headers)
    assert client.get(f"/api/ambulances/{amb}", headers=viewer_headers).json()["gps_source"] == "SIMULATED"
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
    assert STATE.graph is not None


def test_device_gps_rejects_bad_input(client, dispatcher_headers, viewer_headers):
    assert client.post("/api/ambulances/AMB-001/gps", json={"latitude": 200, "longitude": 0},
                       headers=dispatcher_headers).status_code == 422
    assert client.post("/api/ambulances/AMB-404/gps", json={"latitude": 12.97, "longitude": 77.59},
                       headers=dispatcher_headers).status_code == 404
    assert client.post("/api/ambulances/AMB-001/gps", json={"latitude": 12.97, "longitude": 77.59},
                       headers=viewer_headers).status_code == 403
