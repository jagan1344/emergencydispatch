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


class SeverityModel:
    def __init__(self, path: Path = MODEL_PATH):
        self.path = path
        self._bundle = None
        self._lock = threading.Lock()
        self.error: str | None = None

    def load(self) -> bool:
        try:
            self._bundle = joblib.load(self.path)
            clf = self._bundle["pipeline"].named_steps.get("clf")
            if hasattr(clf, "n_jobs"):
                clf.n_jobs = 1   # single-row inference: thread-pool start-up would dominate latency
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
    def version(self) -> str | None:
        return self._bundle["version"] if self._bundle else None

    def predict(self, case: dict) -> Prediction:
        if not self._bundle:
            raise ModelUnavailable(self.error or "model not loaded")
        feats = encode_case(case)
        t0 = time.perf_counter()
        with self._lock:
            pipe = self._bundle["pipeline"]
            proba = pipe.predict_proba(pd.DataFrame([feats], columns=FEATURES))[0]
            classes = list(pipe.classes_)
        probs = {c: round(float(p), 4) for c, p in zip(classes, proba)}
        best = max(probs, key=probs.get)
        return Prediction(best, probs[best], probs, feats, self._bundle["model_name"], self._bundle["version"],
                          (time.perf_counter() - t0) * 1000)

    def metrics(self) -> dict | None:
        try:
            import json
            return json.loads(METRICS_PATH.read_text())
        except FileNotFoundError:
            return None
