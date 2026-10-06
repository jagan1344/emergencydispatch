"""Feature 6 (simulator side): ROUTE_UPDATED continues from the current GPS position - no teleport, no restart."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulator"))

from ambulance_simulator import AmbulanceSimulator  # noqa: E402
from common import haversine_m  # noqa: E402


def seg(a, b, road):
    return {"road_id": road, "length_m": haversine_m(*a, *b), "speed_kph": 36.0, "base_speed_kph": 36.0,
            "coords": [list(a), list(b)]}


A, B, C, D = (0.0, 0.0), (0.0, 0.009), (0.0, 0.018), (0.006, 0.018)   # ~1 km legs


def test_simulator_follows_route_update_without_teleport():
    sim = AmbulanceSimulator("localhost", 1883)
    sim._command("AMB-T", {"command": "FOLLOW_ROUTE", "route_id": "r1", "leg": "TO_PATIENT", "time_scale": 10,
                           "segments": [seg(A, B, "R1"), seg(B, C, "R2"), seg(C, D, "R3")]})
    u = sim.units["AMB-T"]
    msgs = sim.step_unit(u, 3.0)                         # 30 sim-s at 10 m/s = 300 m
    assert msgs[0][1]["event"] == "ROUTE_STARTED"
    before = (u.lat, u.lon)
    assert 250 < haversine_m(*A, *before) < 350
    # backend re-routes from (approximately) the current position: new route starts ~40 m behind the vehicle
    start = (0.0, before[1] - 0.00036)
    E = (-0.006, 0.009)
    sim._command("AMB-T", {"command": "ROUTE_UPDATED", "route_id": "r2", "replaces_route_id": "r1", "leg": "TO_PATIENT",
                           "time_scale": 10, "segments": [seg(start, B, "R1"), seg(B, E, "R9")]})
    m = u.mission
    assert m.route_id == "r2" and m.started                       # no new ROUTE_STARTED, mission continues
    assert (u.lat, u.lon) == before                                # position not reset to the route start
    assert 30 < m.progress_m < 50                                  # snapped onto the new route at the vehicle
    msgs = sim.step_unit(u, 1.0)
    loc = next(p for t, p, _ in msgs if t.endswith("/location"))
    assert loc["route_id"] == "r2" and haversine_m(*before, loc["latitude"], loc["longitude"]) < 110   # continuous
    for _ in range(30):
        msgs = sim.step_unit(u, 1.0)
        if any(p.get("event") == "ARRIVED" for _, p, _ in msgs):
            break
    assert m.arrived and haversine_m(u.lat, u.lon, *E) < 1        # followed the NEW route to its end


def test_follow_route_for_new_leg_starts_at_origin():
    sim = AmbulanceSimulator("localhost", 1883)
    sim._command("AMB-U", {"command": "FOLLOW_ROUTE", "route_id": "x", "leg": "TO_HOSPITAL", "time_scale": 1,
                           "segments": [seg(C, D, "R3")]})
    assert (sim.units["AMB-U"].lat, sim.units["AMB-U"].lon) == C
