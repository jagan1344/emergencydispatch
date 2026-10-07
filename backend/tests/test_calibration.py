"""Severity probability calibration: metrics, the 'do not force it' rule, deployment and the decision trace."""
import joblib
import numpy as np
import pytest

from app.ml.calibration import (MIN_CAL_SAMPLES, brier_multiclass, calibrate, probability_metrics, reliability)
from app.ml.dataset import CLASSES
from tests.conftest import CRITICAL_CASE

C2 = ["A", "B"]


def test_probability_metrics_definitions():
    y = ["A", "B", "A", "B"]
    perfect = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]])
    assert brier_multiclass(y, perfect, C2) == 0.0
    coin = np.full((4, 2), 0.5)
    assert brier_multiclass(y, coin, C2) == pytest.approx(0.5)          # 2 x 0.25 per row
    # confidence 0.8, right 4 of 5 -> perfectly calibrated bin, ECE 0
    y5 = ["A", "A", "A", "A", "B"]
    p5 = np.tile([0.8, 0.2], (5, 1))
    ece, curve = reliability(y5, p5, C2)
    assert ece == pytest.approx(0.0) and curve == [{"bin": [0.7, 0.8], "count": 5, "mean_confidence": 0.8, "accuracy": 0.8}]
    # over-confident: confidence 1.0 but 50 % right -> ECE 0.5
    assert reliability(["A", "B"], np.array([[1.0, 0.0], [1.0, 0.0]]), C2)[0] == pytest.approx(0.5)
    m = probability_metrics(y, perfect, C2)
    assert m["accuracy"] == 1.0 and m["f1_macro"] == 1.0 and m["ece_top_label"] == 0.0


def test_calibration_is_not_forced_on_small_data():
    from app.ml.dataset import generate
    from app.ml.train import build
    from app.ml.dataset import FEATURES
    df = generate(n=600, seed=3)
    x, y = df[FEATURES], df["severity"]
    base = build("random_forest", 0).fit(x[:400], y[:400])
    cal, rep = calibrate(base, x[400:450], y[400:450], x[450:], y[450:])
    assert cal is None and rep["status"] == "UNSUPPORTED" and rep["method"] is None
    assert f">= {MIN_CAL_SAMPLES} rows" in rep["reason"] and "raw probabilities kept" in rep["reason"]
    assert rep["raw"]["brier_multiclass"] > 0                      # raw metrics still reported


def test_deployed_model_reports_raw_and_calibrated_probabilities():
    from app.services.state import STATE
    from app.ml.train import METRICS_PATH
    import json
    report = json.loads(METRICS_PATH.read_text())["calibration"]
    assert report["status"] in ("APPLIED", "NOT_APPLIED", "UNSUPPORTED")
    for key in ("accuracy", "precision_macro", "recall_macro", "f1_macro", "brier_multiclass", "ece_top_label",
                "reliability_curve"):
        assert key in report["raw"]
    pred = STATE.model.predict(CRITICAL_CASE)
    assert sum(pred.probabilities.values()) == pytest.approx(1.0, abs=0.01)
    assert STATE.model.calibration["status"] == report["status"]
    if report["status"] == "APPLIED":
        assert pred.calibration == report["method"] and pred.raw_probabilities is not None
        assert report["calibrated"]["brier_multiclass"] < report["raw"]["brier_multiclass"]
        assert report["calibrated"]["ece_top_label"] < report["raw"]["ece_top_label"]
    else:
        assert pred.calibration is None and pred.raw_confidence == pred.confidence


def test_model_files_without_calibration_still_load(tmp_path):
    from app.ml.dataset import FEATURES, generate
    from app.ml.predict import SeverityModel
    from app.ml.train import build
    df = generate(n=400, seed=5)
    pipe = build("random_forest", 0).fit(df[FEATURES], df["severity"])
    path = tmp_path / "old.joblib"
    joblib.dump({"pipeline": pipe, "classes": CLASSES, "features": FEATURES, "version": "rf-old",
                 "model_name": "RandomForestClassifier", "dataset": "synthetic"}, path)
    m = SeverityModel(path)
    assert m.load()
    p = m.predict(CRITICAL_CASE)
    assert p.calibration is None and p.raw_confidence == p.confidence and m.calibration["status"] == "NOT_EVALUATED"


def test_decision_trace_states_probability_type_and_threshold(client, dispatcher_headers, viewer_headers):
    inc = client.post("/api/emergencies", json={**CRITICAL_CASE, "latitude": 12.9716 + 0.003, "longitude": 77.5946,
                                                "auto_dispatch": False}, headers=dispatcher_headers).json()
    c = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()["confidence"]
    assert "not clinical certainty" in c["label"]
    assert c["probability_type"] in ("calibrated model probability", "raw model probability")
    assert c["threshold_applied"].startswith((">=", "<", "between"))
    assert c["raw_probability"] is not None and 0 < c["confidence"] <= 1
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)


def test_calibration_switch_and_summary_file(monkeypatch):
    import json

    from app.config import get_settings
    from app.ml.train import CALIBRATION_SUMMARY, METRICS_PATH
    from app.services.state import STATE
    rep = json.loads(METRICS_PATH.read_text())["calibration"]
    on = STATE.model.predict(CRITICAL_CASE)
    monkeypatch.setattr(get_settings(), "calibration_enabled", False)       # CALIBRATION_ENABLED=false
    off = STATE.model.predict(CRITICAL_CASE)
    assert off.calibration is None and off.calibrated_confidence is None
    assert off.confidence == off.raw_confidence and off.probabilities == off.raw_probabilities
    if rep["status"] == "APPLIED":
        assert on.calibration_version and on.calibrated_probabilities == on.probabilities
        assert on.raw_probabilities == off.probabilities                       # same raw model underneath
    s = json.loads(CALIBRATION_SUMMARY.read_text())
    for k in ("accuracy", "precision", "recall", "f1", "brier_score_raw", "brier_score_calibrated", "ece_raw",
              "ece_calibrated", "sample_count", "calibration_sample_count", "ece_bins", "split", "status"):
        assert k in s, k
    assert s["sample_count"] > 0 and "untouched test" in s["split"] and s["status"] == rep["status"]


def test_trace_keeps_raw_and_calibrated_vectors(client, dispatcher_headers, viewer_headers):
    inc = client.post("/api/emergencies", json={**CRITICAL_CASE, "latitude": 12.9716 + 0.002, "longitude": 77.5946,
                                                "auto_dispatch": False}, headers=dispatcher_headers).json()
    c = client.get(f"/api/emergencies/{inc['id']}/decision", headers=viewer_headers).json()["confidence"]
    assert c["raw_probabilities"] and abs(sum(c["raw_probabilities"].values()) - 1) < 0.01
    assert c["raw_confidence"] == c["raw_probability"]
    if c["calibration"]:
        assert c["calibrated_probabilities"] and c["calibrated_confidence"] == c["confidence"] and c["calibration_version"]
    client.post(f"/api/emergencies/{inc['id']}/cancel", headers=dispatcher_headers)
