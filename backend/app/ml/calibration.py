"""Probability calibration of the severity classifier + probability-quality metrics.

The RandomForest's class probability is a MODEL CONFIDENCE, not clinical certainty. Calibration only makes that
probability better match the observed frequency on held-out data of the SAME dataset.

Procedure (no data leakage):
  training split (80 %) -> base fit part (75 %) + calibration part (25 %)        test split (20 %) untouched
  base RandomForest fitted on the fit part; calibrator fitted on the calibration part (model frozen):
    * isotonic regression  when the calibration part has >= ISOTONIC_MIN_SAMPLES rows (flexible, needs data)
    * Platt / sigmoid      when it has fewer rows (2 parameters per class, robust for small sets)
    * NOT calibrated       when the calibration part is too small or a class has < MIN_PER_CLASS rows there
  Raw vs calibrated are compared on the untouched test split. The calibrated model is deployed only if it lowers
  BOTH the multiclass Brier score and the expected calibration error without losing more than MAX_ACC_DROP
  accuracy; otherwise the raw model stays deployed and the reason is recorded.
"""
from __future__ import annotations

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import accuracy_score, log_loss, precision_recall_fscore_support

ISOTONIC_MIN_SAMPLES = 1000
MIN_CAL_SAMPLES = 200
MIN_PER_CLASS = 20
MAX_ACC_DROP = 0.01
N_BINS = 10


def brier_multiclass(y_true, proba: np.ndarray, classes: list[str]) -> float:
    onehot = np.array([[1.0 if y == c else 0.0 for c in classes] for y in y_true])
    return float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))


def reliability(y_true, proba: np.ndarray, classes: list[str], n_bins: int = N_BINS) -> tuple[float, list[dict]]:
    """Top-label reliability curve and expected calibration error (ECE, equal-width confidence bins)."""
    conf = proba.max(axis=1)
    pred = np.array(classes)[proba.argmax(axis=1)]
    correct = (pred == np.asarray(y_true)).astype(float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    curve, ece, n = [], 0.0, len(conf)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi) if lo > 0 else (conf >= lo) & (conf <= hi)
        if not m.any():
            continue
        acc, mc = float(correct[m].mean()), float(conf[m].mean())
        ece += m.sum() / n * abs(acc - mc)
        curve.append({"bin": [round(float(lo), 2), round(float(hi), 2)], "count": int(m.sum()),
                      "mean_confidence": round(mc, 4), "accuracy": round(acc, 4)})
    return float(ece), curve


def probability_metrics(y_true, proba: np.ndarray, classes: list[str]) -> dict:
    pred = np.array(classes)[proba.argmax(axis=1)]
    p, r, f, _ = precision_recall_fscore_support(y_true, pred, labels=classes, average="macro", zero_division=0)
    ece, curve = reliability(y_true, proba, classes)
    conf = proba.max(axis=1)
    return {"accuracy": round(float(accuracy_score(y_true, pred)), 4), "precision_macro": round(float(p), 4),
            "recall_macro": round(float(r), 4), "f1_macro": round(float(f), 4),
            "brier_multiclass": round(brier_multiclass(y_true, proba, classes), 4),
            "log_loss": round(float(log_loss(y_true, np.clip(proba, 1e-9, 1), labels=classes)), 4),
            "ece_top_label": round(ece, 4), "reliability_curve": curve,
            "confidence_distribution": {"mean": round(float(conf.mean()), 4),
                                        "p10": round(float(np.percentile(conf, 10)), 4),
                                        "median": round(float(np.median(conf)), 4),
                                        "share_below_0_50": round(float((conf < 0.5).mean()), 4),
                                        "share_below_0_75": round(float((conf < 0.75).mean()), 4)}}


def calibrate(base_pipeline, x_cal, y_cal, x_test, y_test) -> tuple[object | None, dict]:
    """base_pipeline: already fitted on the base-fit part. Returns (calibrated estimator or None, report)."""
    classes = list(base_pipeline.classes_)
    counts = {c: int((np.asarray(y_cal) == c).sum()) for c in classes}
    raw = probability_metrics(y_test, base_pipeline.predict_proba(x_test), classes)
    report: dict = {"calibration_rows": int(len(y_cal)), "calibration_class_counts": counts, "raw": raw}
    if len(y_cal) < MIN_CAL_SAMPLES or min(counts.values()) < MIN_PER_CLASS:
        report.update(status="UNSUPPORTED", method=None,
                      reason=(f"calibration split too small ({len(y_cal)} rows, smallest class {min(counts.values())}; "
                              f"need >= {MIN_CAL_SAMPLES} rows and >= {MIN_PER_CLASS} per class) - raw probabilities kept"))
        return None, report
    method = "isotonic" if len(y_cal) >= ISOTONIC_MIN_SAMPLES else "sigmoid"
    cal = CalibratedClassifierCV(FrozenEstimator(base_pipeline), method=method).fit(x_cal, y_cal)
    calm = probability_metrics(y_test, cal.predict_proba(x_test), classes)
    better = (calm["brier_multiclass"] < raw["brier_multiclass"] and calm["ece_top_label"] < raw["ece_top_label"]
              and calm["accuracy"] >= raw["accuracy"] - MAX_ACC_DROP)
    report.update(method=method, calibrated=calm,
                  status="APPLIED" if better else "NOT_APPLIED",
                  reason=(f"{method} calibration lowered Brier {raw['brier_multiclass']} -> {calm['brier_multiclass']} and "
                          f"ECE {raw['ece_top_label']} -> {calm['ece_top_label']} on the held-out test split"
                          if better else
                          f"{method} calibration did not improve both Brier and ECE on the held-out test split "
                          f"(Brier {raw['brier_multiclass']} -> {calm['brier_multiclass']}, ECE {raw['ece_top_label']} -> "
                          f"{calm['ece_top_label']}) - raw probabilities kept"))
    return (cal if better else None), report
