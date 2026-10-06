"""Pure triage step shared by live intake (services.incident_service.classify) and the evaluation simulator:

    transparent rule score  +  ML severity (class probabilities)  ->  final severity (safety override)
    -> required ambulance capability  ->  confidence-aware decision mode
"""
from __future__ import annotations

from dataclasses import dataclass

from app.dispatch.confidence import ConfidenceAssessment, assess
from app.dispatch.severity import SeverityResult, combine_severity, required_capability, severity_score
from app.ml.predict import ModelUnavailable


@dataclass
class Triage:
    rule: SeverityResult
    prediction: object | None          # app.ml.predict.Prediction, None when the model is unavailable
    ml_error: str | None
    severity: str                      # final severity
    basis: str
    required_capability: str
    assessment: ConfidenceAssessment

    @property
    def ml_level(self) -> str | None:
        return self.prediction.severity if self.prediction is not None else None


def triage(case: dict, model, high: float, low: float) -> Triage:
    rule = severity_score(case)
    pred, err = None, None
    try:
        pred = model.predict(case)
    except ModelUnavailable as exc:
        err = str(exc)
    ml_level = pred.severity if pred is not None else None
    final, basis = combine_severity(ml_level, rule.level)
    ca = assess(pred.probabilities if pred is not None else None, ml_level, high, low,
                safety_override=basis.startswith("SAFETY_OVERRIDE"))
    return Triage(rule, pred, err, final, basis, required_capability(final, case["emergency_type"]), ca)
