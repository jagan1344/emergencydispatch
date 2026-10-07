"""Loads the trained severity model and serves predictions."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import joblib
import pandas as pd

from app.ml.dataset import FEATURES, encode_case
from app.ml.train import METRICS_PATH, MODEL_PATH

log = logging.getLogger("app.ml")


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class Prediction:
    severity: str
    confidence: float
    probabilities: dict[str, float]
    features: dict
    model_name: str
    model_version: str
    latency_ms: float
    # probability calibration (see app/ml/calibration.py): confidence/probabilities above are the DEPLOYED ones
    # (calibrated when calibration was applied); the raw RandomForest values are kept for the decision trace
    raw_probabilities: dict | None = None
    raw_confidence: float | None = None
    calibration: str | None = None          # "isotonic" / "sigmoid" when applied, else None
    calibration_version: str | None = None

    @property
    def calibrated_probabilities(self) -> dict | None:
        return self.probabilities if self.calibration else None

    @property
    def calibrated_confidence(self) -> float | None:
        return self.confidence if self.calibration else None


class SeverityModel:
    def __init__(self, path: Path = MODEL_PATH):
        self.path = path
        self._bundle = None
        self._lock = threading.Lock()
        self.error: str | None = None

    def load(self) -> bool:
        try:
            self._bundle = joblib.load(self.path)
            for pipe in (self._bundle["pipeline"], self._bundle.get("raw_pipeline")):
                for est in _forests(pipe):
                    est.n_jobs = 1   # single-row inference: thread-pool start-up would dominate latency
            self.error = None
            log.info("model loaded", extra={"event": "ML_MODEL_LOADED", "fields": {"version": self.version}})
            return True
        except Exception as exc:  # file missing / incompatible pickle
            self._bundle = None
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            log.error("model unavailable", extra={"event": "ML_MODEL_UNAVAILABLE", "fields": {"error": self.error}})
            return False

    @property
    def available(self) -> bool:
        return self._bundle is not None

    @property
    def dataset(self) -> str | None:
        return self._bundle.get("dataset", "synthetic") if self._bundle else None

    @property
    def version(self) -> str | None:
        return self._bundle["version"] if self._bundle else None

    def predict(self, case: dict) -> Prediction:
        if not self._bundle:
            raise ModelUnavailable(self.error or "model not loaded")
        columns = self._bundle.get("features", FEATURES)
        if self._bundle.get("dataset", "synthetic") == "synthetic":
            feats = encode_case(case)
        else:
            from app.ml.real_datasets import encode_case_real
            feats = encode_case_real(case)
        t0 = time.perf_counter()
        with self._lock:
            pipe = self._bundle["pipeline"]
            frame = pd.DataFrame([feats], columns=columns)
            if self._bundle.get("dataset", "synthetic") != "synthetic":
                frame = frame.astype(float)
            proba = pipe.predict_proba(frame)[0]
            classes = list(pipe.classes_)
            raw_pipe = self._bundle.get("raw_pipeline")
            raw = dict(zip(list(raw_pipe.classes_), raw_pipe.predict_proba(frame)[0])) if raw_pipe is not None else None
            if raw is not None and not _calibration_enabled():     # CALIBRATION_ENABLED=false -> decide on raw
                proba, classes, raw = [raw[c] for c in raw], list(raw), None
        probs = {c: round(float(p), 4) for c, p in zip(classes, proba)}
        best = max(probs, key=probs.get)
        cal = self._bundle.get("calibration") or {}
        raw_probs = {c: round(float(p), 4) for c, p in raw.items()} if raw else None
        applied = raw is not None and cal.get("status") == "APPLIED"
        return Prediction(best, probs[best], probs, feats, self._bundle["model_name"], self._bundle["version"],
                          (time.perf_counter() - t0) * 1000, raw_probs or probs, raw_probs[best] if raw_probs else probs[best],
                          cal.get("method") if applied else None, cal.get("version") if applied else None)

    @property
    def calibration(self) -> dict:
        return dict((self._bundle or {}).get("calibration") or {"status": "NOT_EVALUATED", "method": None,
                                                               "reason": "model trained before calibration support"})

    def metrics(self) -> dict | None:
        try:
            import json
            return json.loads(METRICS_PATH.read_text())
        except FileNotFoundError:
            return None


def _forests(est) -> list:
    """Every fitted estimator with n_jobs inside a pipeline / calibrated wrapper."""
    out, stack = [], [est]
    while stack:
        e = stack.pop()
        if e is None:
            continue
        if hasattr(e, "n_jobs") and not hasattr(e, "calibrated_classifiers_"):
            out.append(e)
        if hasattr(e, "named_steps"):
            stack += list(e.named_steps.values())
        for cc in getattr(e, "calibrated_classifiers_", []):
            stack.append(getattr(cc, "estimator", None))
        if hasattr(e, "estimator"):
            stack.append(e.estimator)
    return out


def _calibration_enabled() -> bool:
    from app.config import get_settings
    return get_settings().calibration_enabled
