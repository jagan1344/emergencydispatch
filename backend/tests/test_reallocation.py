"""Feature 5 - dynamic multi-emergency resource reallocation (priority-aware, never unsafe)."""
import pytest

from app.config import get_settings
from app.services.dispatch_service import dispatcher_cycle
from app.services.mission_service import handle_sim_status
from app.services.routes_service import ACTIVE
from app.services.state import STATE
from app.database import session_scope
from app.utils.geo import haversine_m
from tests.conftest import CRITICAL_CASE, MILD_CASE


@pytest.fixture
def fleet(client, admin_headers, dispatcher_headers):
    """Only two units on duty: U (ICU, will serve a LOW incident) and R (the farthest other unit)."""
    ambs = client.get("/api/ambulances", headers=admin_headers).json()
    u = next(a for a in ambs if a["equipment_level"] == "ICU")
    r = max((a for a in ambs if a["id"] != u["id"]),
            key=lambda a: haversine_m(a["latitude"], a["longitude"], u["latitude"], u["longitude"]))
    for a in ambs:
        if a["id"] != u["id"]:
            client.patch(f"/api/ambulances/{a['id']}", json={"status": "OFFLINE"}, headers=admin_headers)
    created = []
    yield u, r, ambs, created
    for iid in created:
        client.post(f"/api/emergencies/{iid}/cancel", headers=dispatcher_headers)
    for a in ambs:
        client.patch(f"/api/ambulances/{a['id']}", json={"status": "AVAILABLE"}, headers=admin_headers)


def _new(client, headers, case, lat, lon, created):
    inc = client.post("/api/emergencies", json={**case, "latitude": lat, "longitude": lon, "auto_dispatch": False},
                      headers=headers).json()
    created.append(inc["id"])
    return inc


def _donor_on_the_way(client, headers, u, created):
    """LOW incident ~1 km from U, dispatched to U (only unit on duty), U starts driving."""
    d = _new(client, headers, MILD_CASE, u["latitude"] + 0.009, u["longitude"], created)
    dispatcher_cycle()
    d = client.get(f"/api/emergencies/{d['id']}", headers=headers).json()
    assert d["assigned_ambulance"] == u["id"]
    ar = ACTIVE.get(u["id"])
    with STATE.lock, session_scope() as db:
        handle_sim_status(db, u["id"], {"event": "ROUTE_STARTED", "route_id": str(ar.route_id)})
    return d


def _policy(monkeypatch, max_delay):
    st = get_settings()
    monkeypatch.setattr(st, "resource_reallocation_threshold_s", 1.0)   # any free unit counts as "too far"
    monkeypatch.setattr(st, "reallocation_min_gain_s", 0.0)
    monkeypatch.setattr(st, "reallocation_max_donor_delay_s", max_delay)


def test_critical_incident_takes_unit_from_low_priority_with_replacement(client, dispatcher_headers, admin_headers,
                                                                         viewer_headers, fleet, monkeypatch):
    u, r, _, created = fleet
    _policy(monkeypatch, 1e6)
    donor = _donor_on_the_way(client, dispatcher_headers, u, created)
    client.patch(f"/api/ambulances/{r['id']}", json={"status": "AVAILABLE"}, headers=admin_headers)   # replacement
    crit = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] - 0.002, u["longitude"] + 0.001, created)
    dispatcher_cycle()
    c = client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()
    d = client.get(f"/api/emergencies/{donor['id']}", headers=viewer_headers).json()
    assert c["assigned_ambulance"] == u["id"] and c["status"] == "EN_ROUTE"          # U diverted, keeps driving
    assert d["assigned_ambulance"] == r["id"] and d["status"] == "DISPATCHED"         # donor got the replacement
    conf = client.get("/api/dispatch/conflicts", headers=viewer_headers).json()[0]
    assert conf["decision"] == "REALLOCATED" and conf["ambulance_id"] == u["id"]
    assert conf["from_incident_id"] == donor["id"] and conf["to_incident_id"] == crit["id"]
    assert conf["impact_s"] is not None and "outranks" in conf["reason"]
    assert any(e["type"] == "RESOURCE_REALLOCATED" for e in c["timeline"])
    assert ACTIVE.get(u["id"]).incident_id.hex == crit["id"].replace("-", "")
    assert c["dispatch"]["method"] == "REALLOCATION"


def test_large_donor_impact_is_escalated_then_approved(client, dispatcher_headers, viewer_headers, fleet, monkeypatch):
    u, r, _, created = fleet
    _policy(monkeypatch, 0.0)                         # no delay allowed -> must escalate (no replacement anyway)
    donor = _donor_on_the_way(client, dispatcher_headers, u, created)
    crit = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] - 0.002, u["longitude"] + 0.001, created)
    dispatcher_cycle()
    dispatcher_cycle()                                # must not escalate the same conflict twice
    c = client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()
    assert c["status"] == "WAITING" and "needs dispatcher approval" in c["dispatch_note"]
    open_ = client.get("/api/dispatch/conflicts?open_only=true", headers=viewer_headers).json()
    assert len(open_) == 1 and open_[0]["decision"] == "ESCALATED" and "no replacement" in open_[0]["reason"]
    assert client.post(f"/api/dispatch/conflicts/{open_[0]['id']}/approve", headers=viewer_headers).status_code == 403
    res = client.post(f"/api/dispatch/conflicts/{open_[0]['id']}/approve", headers=dispatcher_headers).json()
    assert res["decision"] == "APPROVED" and res["resolved_by"] == "dispatcher" and res["id"] == open_[0]["id"]
    assert len([c for c in client.get("/api/dispatch/conflicts", headers=viewer_headers).json()
                if c["to_incident_id"] == crit["id"]]) == 1                       # resolved in place, no duplicate
    dec = client.get(f"/api/emergencies/{crit['id']}/decision", headers=viewer_headers).json()
    assert "reallocated from a lower-priority incident" in dec["dispatch"]["explanation"]["summary"]
    assert client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()["assigned_ambulance"] == u["id"]
    d = client.get(f"/api/emergencies/{donor['id']}", headers=viewer_headers).json()
    assert d["status"] == "WAITING" and d["assigned_ambulance"] is None    # no free unit: back in the queue


def test_never_steals_from_equally_or_more_critical(client, dispatcher_headers, viewer_headers, fleet, monkeypatch):
    u, r, _, created = fleet
    _policy(monkeypatch, 1e6)
    d = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] + 0.009, u["longitude"], created)
    dispatcher_cycle()
    assert client.get(f"/api/emergencies/{d['id']}", headers=viewer_headers).json()["assigned_ambulance"] == u["id"]
    crit = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] - 0.002, u["longitude"] + 0.001, created)
    dispatcher_cycle()
    c = client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()
    assert c["status"] == "WAITING" and c["assigned_ambulance"] is None
    assert client.get(f"/api/emergencies/{d['id']}", headers=viewer_headers).json()["assigned_ambulance"] == u["id"]
    assert all(x["to_incident_id"] != crit["id"] for x in client.get("/api/dispatch/conflicts", headers=viewer_headers).json())


def test_contested_unit_goes_to_higher_priority(client, dispatcher_headers, admin_headers, viewer_headers, fleet):
    u, r, _, created = fleet
    client.patch(f"/api/ambulances/{r['id']}", json={"status": "AVAILABLE"}, headers=admin_headers)
    hi = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] + 0.002, u["longitude"], created)
    lo = _new(client, dispatcher_headers, {**CRITICAL_CASE, "consciousness": "ALERT", "bleeding": "MINOR",
                                          "injury_severity": "MINOR", "breathing_difficulty": False, "heart_rate": 105},
              u["latitude"] + 0.002, u["longitude"] + 0.001, created)
    dispatcher_cycle()
    h = client.get(f"/api/emergencies/{hi['id']}", headers=viewer_headers).json()
    lw = client.get(f"/api/emergencies/{lo['id']}", headers=viewer_headers).json()
    assert h["priority"] > lw["priority"] and h["assigned_ambulance"] == u["id"] and lw["assigned_ambulance"] == r["id"]
    conf = [c for c in client.get("/api/dispatch/conflicts", headers=viewer_headers).json() if c["kind"] == "CONTESTED_UNIT"]
    assert conf and conf[0]["decision"] == "ASSIGN_OTHER" and conf[0]["from_incident_id"] == hi["id"]


def test_escalation_while_free_unit_serves_then_approved_swap(client, dispatcher_headers, admin_headers, viewer_headers,
                                                              fleet, monkeypatch):
    """Requester is served by the free (slow) unit meanwhile; approving swaps the two units."""
    u, r, _, created = fleet
    _policy(monkeypatch, 0.0)
    donor = _donor_on_the_way(client, dispatcher_headers, u, created)
    client.patch(f"/api/ambulances/{r['id']}", json={"status": "AVAILABLE"}, headers=admin_headers)
    crit = _new(client, dispatcher_headers, CRITICAL_CASE, u["latitude"] - 0.002, u["longitude"] + 0.001, created)
    dispatcher_cycle()
    c = client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()
    assert c["assigned_ambulance"] == r["id"]                         # best free unit sent meanwhile (never left waiting)
    esc = client.get("/api/dispatch/conflicts?open_only=true", headers=viewer_headers).json()
    assert len(esc) == 1 and esc[0]["ambulance_id"] == u["id"] and "delayed by" in esc[0]["reason"]
    res = client.post(f"/api/dispatch/conflicts/{esc[0]['id']}/approve", headers=dispatcher_headers)
    assert res.status_code == 200, res.text
    assert "swap" in res.json()["reason"]
    c = client.get(f"/api/emergencies/{crit['id']}", headers=viewer_headers).json()
    d = client.get(f"/api/emergencies/{donor['id']}", headers=viewer_headers).json()
    assert c["assigned_ambulance"] == u["id"] and d["assigned_ambulance"] == r["id"]
    assert ACTIVE.get(u["id"]).incident_id.hex == crit["id"].replace("-", "")
    assert ACTIVE.get(r["id"]).incident_id.hex == donor["id"].replace("-", "")
    assert client.post(f"/api/dispatch/conflicts/{esc[0]['id']}/approve", headers=dispatcher_headers).status_code == 409
