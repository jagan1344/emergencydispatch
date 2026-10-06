"""Feature 2 - explainable and counterfactual dispatch decisions (derived from real decision data)."""
import pytest

from app.dispatch.explain import counterfactual, explain_selection, factor_ratings
from app.dispatch.scoring import CandidateInput, score_candidates
from tests.conftest import CRITICAL_CASE


def ranked():
    cands = [
        CandidateInput("AMB-A", "ADVANCED", eta_s=492, distance_m=5700, traffic_delay_s=60, missions_today=0,
                       fuel_level=82, extra={"fuel_level": 82, "missions_today": 0}),
        CandidateInput("AMB-B", "ADVANCED", eta_s=696, distance_m=7900, traffic_delay_s=150, missions_today=4,
                       fuel_level=40, extra={"fuel_level": 40, "missions_today": 4}),
        CandidateInput("AMB-C", "BASIC", eta_s=300, distance_m=2500, traffic_delay_s=10, missions_today=0,
                       fuel_level=90, extra={"fuel_level": 90, "missions_today": 0}),
    ]
    return [c.as_dict() for c in score_candidates(cands, "ADVANCED")]


def test_selected_explanation_from_actual_values():
    r = ranked()
    assert r[0]["ambulance_id"] == "AMB-A"
    x = explain_selection(r, "AMB-A")
    assert x["runner_up"] == "AMB-B"                    # unsuitable AMB-C is not the runner-up
    assert any("3.4 min lower ETA" in s for s in x["reasons"])
    assert any("lower workload (0 vs 4" in s for s in x["reasons"])
    assert any("more fuel (82% vs 40%)" in s for s in x["reasons"])
    assert sum(x["contributions"].values()) == pytest.approx(r[0]["score"], abs=1e-3)
    fr = {f["factor"]: f for f in factor_ratings(r[0])}
    assert fr["capability"]["status"] == "ok" and fr["fuel"]["status"] == "ok" and fr["workload"]["status"] == "ok"


def test_counterfactuals_and_why_not():
    r = ranked()
    sel = r[0]
    cf_b = counterfactual(sel, next(c for c in r if c["ambulance_id"] == "AMB-B"))
    assert cf_b["eta_delta_s"] == pytest.approx(204, abs=0.5)
    assert cf_b["score_delta"] > 0 and cf_b["worse_on"][0] == "eta"
    assert "AMB-B scores" in cf_b["why_not"] and "later" in cf_b["why_not"]
    # deltas add up to the score difference
    assert sum(cf_b["factor_deltas"].values()) == pytest.approx(cf_b["score_delta"], abs=2e-3)
    cf_c = counterfactual(sel, next(c for c in r if c["ambulance_id"] == "AMB-C"))
    assert "unsuitable" in cf_c["why_not"] and cf_c["eta_delta_s"] < 0      # faster but unsuitable
    assert any("ETA" in t for t in cf_c["tradeoffs"])


def test_decision_api_exposes_explanation(client, dispatcher_headers, viewer_headers):
    p = {**CRITICAL_CASE, "latitude": 12.9716 - 0.003, "longitude": 77.5946 + 0.004, "auto_dispatch": True}
    inc = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
    d = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()
    x = d["dispatch"]["explanation"]
    assert x["selected"] == inc["assigned_ambulance"] and x["summary"].startswith(inc["assigned_ambulance"])
    assert len(x["counterfactuals"]) == d["dispatch"]["candidates_considered"] - 1
    alt = x["counterfactuals"][0]["ambulance_id"]
    w = client.get(f"/api/emergencies/{inc['id']}/alternatives?ambulance_id={alt}", headers=viewer_headers).json()
    assert w["ambulance_id"] == alt and w["why_not"]
    assert client.get(f"/api/emergencies/{inc['id']}/alternatives?ambulance_id=AMB-999",
                      headers=viewer_headers).status_code == 404
    assert d["confidence"]["decision_mode"] == "AUTO_DISPATCH"
    assert [t["event"] for t in d["trace"]][:3] == ["EMERGENCY_CREATED", "EMERGENCY_STATUS_CHANGED", "EMERGENCY_CLASSIFIED"]
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)


def test_tradeoff_is_stated_when_runner_up_is_faster():
    cands = [CandidateInput("ICU-1", "ICU", eta_s=390, distance_m=4400, traffic_delay_s=10, missions_today=0, fuel_level=90,
                            extra={"fuel_level": 90, "missions_today": 0}),
             CandidateInput("ADV-1", "ADVANCED", eta_s=300, distance_m=3800, traffic_delay_s=120, missions_today=0,
                            fuel_level=90, extra={"fuel_level": 90, "missions_today": 0})]
    r = [c.as_dict() for c in score_candidates(cands, "ICU")]
    x = explain_selection(r, r[0]["ambulance_id"])
    assert r[0]["ambulance_id"] == "ICU-1" and "Trade-off: ADV-1 would have arrived 1.5 min earlier" in x["summary"]


def test_decision_api_survives_infinite_predicted_eta(client, dispatcher_headers, viewer_headers):
    """Regression: a road ahead predicted BLOCKED made the predicted ETA infinite -> HTTP 500 (not JSON compliant)."""
    from app.services.routes_service import ACTIVE
    from app.services.state import STATE
    from app.utils.geo import haversine_m
    ambs = [a for a in client.get("/api/ambulances", headers=viewer_headers).json() if a["status"] == "AVAILABLE"]
    g = STATE.graph
    nodes = [(float(g.lat[i]), float(g.lon[i])) for i in range(0, len(g.lat), 7)
             if haversine_m(12.9716, 77.5946, float(g.lat[i]), float(g.lon[i])) < 2000]
    lat, lon = max(nodes, key=lambda n: min(haversine_m(*n, a["latitude"], a["longitude"]) for a in ambs))
    p = {**CRITICAL_CASE, "latitude": lat, "longitude": lon, "auto_dispatch": True}
    inc = client.post("/api/emergencies", json=p, headers=dispatcher_headers).json()
    ar = ACTIVE.get(inc["assigned_ambulance"])
    assert len([s for s in ar.segments if s.road_id]) >= 3, (inc["status"], inc["assigned_ambulance"], ar.leg,
                                                             len(ar.segments), [(s.road_id, round(s.length_m)) for s in ar.segments][:5])
    road = [s.road_id for s in ar.segments if s.road_id][-1]      # a road later on the route
    STATE.graph.set_road_prediction(road, "BLOCKED")
    try:
        r = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers)
        assert r.status_code == 200 and r.json()["traffic"]["predicted_eta_s"] is None
    finally:
        STATE.graph.set_road_prediction(road, None)
        client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
