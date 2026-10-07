"""Decision trace: summary of what was considered, live route state and structured rejection codes."""
from tests.conftest import MILD_CASE

AMB_CODES = {"UNSUITABLE_CAPABILITY", "ASSIGNED_TO_HIGHER_PRIORITY", "MANUAL_OVERRIDE", "UNAVAILABLE_AT_COMMIT",
             "TOO_SLOW", "HEAVY_TRAFFIC", "LOWER_CAPABILITY_MATCH", "HIGHER_WORKLOAD", "LOW_FUEL", "LONGER_DISTANCE",
             "HIGHER_SCORE", "NO_DRIVABLE_ROUTE", "NO_ROUTE"}
HOSP_CODES = {"LACKS_CAPABILITY", "CAPABILITY_UNKNOWN", "HOSPITAL_FULL", "TOO_SLOW", "HOSPITAL_CONGESTION", "HIGHER_SCORE"}


def test_decision_summary_route_state_and_rejection_codes(client, dispatcher_headers, viewer_headers):
    client.post("/api/simulation/reset", json={}, headers=dispatcher_headers)
    inc = client.post("/api/emergencies", json={**MILD_CASE, "latitude": 12.9716 - 0.004, "longitude": 77.5946 + 0.005,
                                                "auto_dispatch": True}, headers=dispatcher_headers).json()
    try:
        dec = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()
        s = dec["summary"]
        assert set(s["considered"]) >= {"severity_model", "ambulance_candidates", "hospital_candidates",
                                        "reallocation_considered", "rerouting_considered", "reroutes_applied"}
        assert dec["dispatch"] is not None, dec["incident"]
        assert s["considered"]["ambulance_candidates"] == dec["dispatch"]["candidates_considered"] >= 1
        assert s["why"]["ambulance"] and s["constraints"]
        for cf in dec["dispatch"]["explanation"]["counterfactuals"]:
            assert cf["rejection_codes"] and set(cf["rejection_codes"]) <= AMB_CODES, cf["rejection_codes"]
        for u in dec["dispatch"]["explanation"].get("unreachable") or []:
            assert set(u["rejection_codes"]) <= AMB_CODES
        h = dec["hospital"]
        if h and h.get("counterfactual"):
            for w in h["counterfactual"]["why_not"]:
                assert w["rejection_codes"] and set(w["rejection_codes"]) <= HOSP_CODES, w["rejection_codes"]
        active = [r for r in dec["routes"] if r["active"]]
        if active:
            assert active[0]["status"] in ("ACTIVE", "UNAVAILABLE") and s["route_status"] == active[0]["status"]
            assert "checkpoint" in active[0] and active[0]["remaining_m"] is not None
    finally:
        client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
