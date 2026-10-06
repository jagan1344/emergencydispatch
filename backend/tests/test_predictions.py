"""Features 3 + 4: predictive traffic and predictive hospital congestion."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.database import get_engine, session_scope
from app.dispatch.scoring import HospitalInput, score_hospitals
from app.ml.traffic_model import Stats, fallback_predict
from app.routing.engine import RoutingEngine, predicted_duration
from app.services.hospital_prediction import expected_wait_min, forecast
from app.services.state import STATE
from app.services.traffic_prediction import PREDICTOR
from app.utils.timeutil import utcnow
from tests.test_routing import diamond


@pytest.fixture
def clean_predictions():
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM traffic_events WHERE source = 'TEST_HISTORY'"))
        conn.execute(text("DELETE FROM traffic_predictions"))
    for rid in STATE.graph.road_ids:
        if STATE.graph.has_pred[STATE.graph.road_idx[rid]]:
            STATE.graph.set_road_prediction(rid, None)
    PREDICTOR.model = None


# ------------------------------------------------------------------ traffic
def test_fallback_rules():
    st = Stats(tau_min=10, incident_min=30, p_persist=0.7)
    assert fallback_predict(4, False, True, age_min=5, road_mean=0.5, horizon=10, st=st) == (4, 0.7)   # accident persists
    lvl, conf = fallback_predict(4, False, True, age_min=25, road_mean=0.5, horizon=10, st=st)        # clears in 5 min
    assert lvl < 4 and conf == pytest.approx(0.3)
    lvl, _ = fallback_predict(3, False, False, age_min=0, road_mean=0.0, horizon=10, st=st)           # mean reversion
    assert lvl == 1           # 3 + (0-3)(1-e^-1) = 1.10


def test_fallback_when_history_is_insufficient(clean_predictions):
    m = PREDICTOR.ensure_model(force=True)
    assert m.method == "FALLBACK" and "insufficient" in m.metrics["reason"]


def _insert_history(roads, cycles=12):
    """Deterministic periodic pattern: accident (SEVERE) for 20 sim-min, then FREE for 40 sim-min."""
    scale = get_settings().sim_time_scale
    now = utcnow()
    rows = []
    for j, rid in enumerate(roads):
        for c in range(cycles):
            t0 = (cycles - c) * 60 + (j % 7)                      # sim minutes ago
            rows.append({"r": rid, "t": now - timedelta(seconds=t0 * 60 / scale), "lvl": "SEVERE", "et": "ACCIDENT"})
            rows.append({"r": rid, "t": now - timedelta(seconds=(t0 - 20) * 60 / scale), "lvl": "FREE", "et": "CLEAR"})
    with get_engine().begin() as conn:
        conn.execute(text("INSERT INTO traffic_events(road_id, created_at, event_type, new_level, blocked, source, active) "
                          "VALUES (:r, :t, :et, :lvl, false, 'TEST_HISTORY', false)"), rows)


def test_model_trained_stored_and_beats_persistence(clean_predictions, client, dispatcher_headers, viewer_headers,
                                                     monkeypatch):
    monkeypatch.setattr(get_settings(), "traffic_model_min_samples", 200)
    roads = STATE.graph.road_ids[:40]
    _insert_history(roads)
    r = client.post("/api/traffic/predictions/refresh?retrain=true", headers=dispatcher_headers).json()
    m = r["model"]
    assert m["method"] == "MODEL", m["metrics"]
    assert m["metrics"]["accuracy_model"] > m["metrics"]["accuracy_persistence"]
    assert m["model_version"].startswith("traffic-rf-")
    # an accident that started 15 sim-min ago is predicted to have cleared within the 10-min horizon
    from app.services.traffic_service import apply_update
    with session_scope() as db:
        apply_update(db, roads[0], level="SEVERE", incident_multiplier=0.5, event_type="ACCIDENT", source="TEST")
        db.execute(text("UPDATE road_conditions SET updated_at = now() - interval '15 seconds' WHERE road_id = :r"),
                   {"r": roads[0]})
    rows = PREDICTOR.predict({roads[0]})
    assert rows[0]["current_level"] == "SEVERE" and rows[0]["predicted_level"] != "SEVERE"
    assert rows[0]["method"] == "MODEL" and 0 < rows[0]["confidence"] <= 1
    stored = client.get("/api/traffic/predictions", headers=viewer_headers).json()["predictions"]
    assert any(p["road_id"] == roads[0] and p["predicted_level"] == rows[0]["predicted_level"] for p in stored)
    assert STATE.graph.has_pred[STATE.graph.road_idx[roads[0]]]
    with session_scope() as db:
        apply_update(db, roads[0], level="FREE", blocked=False, incident_multiplier=1.0, event_type="CLEAR", source="TEST")


def test_prediction_used_in_route_selection(monkeypatch):
    monkeypatch.setattr(get_settings(), "traffic_prediction_horizon_min", 0.5)    # 30 s horizon for a 1 km network
    g = diamond()
    eng = RoutingEngine(g)
    now = eng.route((0.0, 0.0), (0.0, 0.010))
    assert "R1" in now.road_ids()                                  # top road is fastest right now
    g.set_road_prediction("R1", "SEVERE")                          # ...but predicted to jam
    r = eng.route((0.0, 0.0), (0.0, 0.010))
    assert "R2" in r.road_ids() and r.engine == "graph-predicted"
    top = next(a for a in r.alternatives if a["engine"] == "graph")
    assert top["predicted_duration_s"] > r.predicted_duration_s     # current-best route is worse under prediction
    assert r.adjusted_duration_s > top["adjusted_duration_s"]       # chosen despite a higher current-traffic ETA


def test_predicted_duration_blend():
    g = diamond()
    eng = RoutingEngine(g)
    g.set_road_prediction("R1", "HEAVY")
    segs = eng.route((0.0, 0.0), (0.0, 0.010)).segments
    assert predicted_duration(segs, 1e9) == pytest.approx(sum(s.adj_time_s for s in segs), rel=1e-6)   # far horizon -> now
    assert predicted_duration(segs, 1e-9) == pytest.approx(25 + sum(s.pred_time_s for s in segs[1:]), rel=0.05)


# ------------------------------------------------------------------ hospitals
def test_hospital_forecast_formula():
    f = forecast(current_load=9, capacity=20, horizon_min=10, incoming_etas_min=[4, 12], arrivals=0, discharges=0,
                 window_min=240, default_stay_min=240, slot_min=5, max_wait=120, hospital_id="H")
    assert f.method == "DEFAULT_RATES" and f.confidence == "LOW" and f.incoming_ambulances == 1
    assert f.predicted_load == pytest.approx(9 * 2.718281828 ** (-10 / 240) + 1, rel=1e-3)
    assert f.expected_wait_min == pytest.approx(expected_wait_min(f.predicted_load, 20, 5, 120), abs=0.02)
    busy = forecast(18, 20, 10, [1, 2, 3], arrivals=24, discharges=6, window_min=240, default_stay_min=240,
                    slot_min=5, max_wait=120)
    assert busy.method == "OBSERVED_RATES" and busy.predicted_load_pct > 100 and busy.expected_wait_min > 30
    assert expected_wait_min(100, 10, 5, 120) <= 120


def test_slightly_farther_hospital_with_shorter_wait_wins():
    a = HospitalInput("A", "Near but congested", eta_s=360, traffic_delay_s=0, distance_m=4000, current_load=9,
                      emergency_capacity=20, icu_available=2, trauma=True, cardiac=True, stroke=True,
                      predicted_load=16.4, expected_wait_s=15 * 60)
    b = HospitalInput("B", "Farther, free", eta_s=480, traffic_delay_s=0, distance_m=6000, current_load=8,
                      emergency_capacity=20, icu_available=2, trauma=True, cardiac=True, stroke=True,
                      predicted_load=10.2, expected_wait_s=3 * 60)
    ranked = score_hospitals([a, b], ["icu"])
    assert ranked[0]["hospital_id"] == "B"
    assert ranked[0]["time_to_treatment_s"] == 660 and ranked[1]["time_to_treatment_s"] == 1260
    # capability constraint still dominates: B without ICU loses for an ICU patient
    b.icu_available = 0
    assert score_hospitals([a, b], ["icu"])[0]["hospital_id"] == "A"


def test_hospital_prediction_api(client, viewer_headers):
    r = client.get("/api/hospitals/predictions", headers=viewer_headers).json()
    assert r["estimated"] is True and len(r["predictions"]) >= 1
    p = r["predictions"][0]
    assert {"current_load", "predicted_load", "predicted_load_pct", "expected_wait_min", "method",
            "model_version"} <= set(p)
    one = client.get(f"/api/hospitals/{p['hospital_id']}/prediction?horizon_min=30", headers=viewer_headers).json()
    assert one["horizon_min"] == 30
    assert client.get("/api/hospitals/HSP-999/prediction", headers=viewer_headers).status_code == 404
