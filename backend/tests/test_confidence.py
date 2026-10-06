"""Feature 1 - confidence-aware dispatch."""
import pytest

from app.config import get_settings
from app.dispatch.confidence import assess
from tests.conftest import CRITICAL_CASE

P = {"LOW": 0.05, "MEDIUM": 0.10, "HIGH": 0.80, "CRITICAL": 0.05}


def test_assessment_bands():
    hi = assess(P, "HIGH", 0.75, 0.50)
    assert (hi.confidence_level, hi.decision_mode, hi.confidence) == ("HIGH", "AUTO_DISPATCH", 0.8)
    mid = assess({"HIGH": 0.6, "CRITICAL": 0.3, "MEDIUM": 0.1}, "HIGH", 0.75, 0.50)
    assert (mid.confidence_level, mid.decision_mode, mid.runner_up) == ("MEDIUM", "DISPATCH_WITH_REVIEW", "CRITICAL")
    assert mid.margin == pytest.approx(0.3)
    lo = assess({"HIGH": 0.43, "CRITICAL": 0.40, "MEDIUM": 0.17}, "HIGH", 0.75, 0.50)
    assert (lo.confidence_level, lo.decision_mode) == ("LOW", "HUMAN_REVIEW")
    assert "below" in lo.reason
    assert assess(None, None, 0.75, 0.5).decision_mode == "DISPATCH_WITH_REVIEW"     # model unavailable
    assert assess(P, "HIGH", 0.75, 0.5, safety_override=True).decision_mode == "DISPATCH_WITH_REVIEW"
    with pytest.raises(ValueError):
        assess(P, "HIGH", 0.4, 0.6)


def _create(client, headers, **extra):
    p = {**CRITICAL_CASE, "latitude": 12.9716 + 0.002, "longitude": 77.5946 + 0.002, "auto_dispatch": True, **extra}
    r = client.post("/api/emergencies", json=p, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_high_confidence_auto_dispatch(client, dispatcher_headers):
    inc = _create(client, dispatcher_headers)
    assert inc["confidence_level"] == "HIGH" and inc["decision_mode"] == "AUTO_DISPATCH"
    assert inc["status"] == "DISPATCHED" and sum(inc["class_probabilities"].values()) == pytest.approx(1, abs=1e-3)
    assert any(e["type"] == "CONFIDENCE_ASSESSED" for e in inc["timeline"])
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)


def test_medium_confidence_dispatches_with_review_flag(client, dispatcher_headers, monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "dispatch_confidence_high", 0.9999)
    monkeypatch.setattr(st, "dispatch_confidence_low", 0.01)
    inc = _create(client, dispatcher_headers)
    assert inc["confidence_level"] == "MEDIUM" and inc["decision_mode"] == "DISPATCH_WITH_REVIEW"
    assert inc["status"] == "DISPATCHED"
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)


def test_low_confidence_waits_for_human_review(client, dispatcher_headers, viewer_headers, monkeypatch):
    from app.services.dispatch_service import dispatcher_cycle
    st = get_settings()
    monkeypatch.setattr(st, "dispatch_confidence_high", 0.99999)
    monkeypatch.setattr(st, "dispatch_confidence_low", 0.99998)
    monkeypatch.setattr(st, "human_review_timeout_s", 0)       # no safety release during this test
    inc = _create(client, dispatcher_headers)
    assert inc["confidence_level"] == "LOW" and inc["decision_mode"] == "HUMAN_REVIEW"
    dispatcher_cycle()
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "WAITING" and "awaiting human review" in d["dispatch_note"]
    assert client.post(f"/api/emergencies/{inc['id']}/review", json={"severity": "CRITICAL"},
                       headers=viewer_headers).status_code == 403
    r = client.post(f"/api/emergencies/{inc['id']}/review", json={"severity": "CRITICAL"}, headers=dispatcher_headers)
    d = r.json()
    assert d["decision_mode"] == "HUMAN_APPROVED" and d["reviewed_by"] == "dispatcher"
    assert d["status"] == "DISPATCHED" and d["required_capability"] == "ICU"
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)


def test_human_review_safety_timeout(client, dispatcher_headers, viewer_headers, monkeypatch):
    from app.services.dispatch_service import dispatcher_cycle
    st = get_settings()
    monkeypatch.setattr(st, "dispatch_confidence_high", 0.99999)
    monkeypatch.setattr(st, "dispatch_confidence_low", 0.99998)
    monkeypatch.setattr(st, "human_review_timeout_s", 0.001)
    inc = _create(client, dispatcher_headers, auto_dispatch=False)
    dispatcher_cycle()
    d = client.get(f"/api/emergencies/{inc['id']}", headers=viewer_headers).json()
    assert d["status"] == "DISPATCHED" and d["decision_mode"] == "DISPATCH_WITH_REVIEW"
    assert any(e["type"] == "HUMAN_REVIEW_TIMEOUT" for e in d["timeline"])
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
