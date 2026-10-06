"""Confidence-aware dispatch decision.

The severity model returns class probabilities; the probability of the predicted class is its confidence.
Thresholds are configuration (DISPATCH_CONFIDENCE_HIGH / DISPATCH_CONFIDENCE_LOW):

    confidence >= HIGH                 -> AUTO_DISPATCH
    LOW <= confidence < HIGH           -> DISPATCH_WITH_REVIEW   (dispatched now, flagged for a dispatcher)
    confidence < LOW                   -> HUMAN_REVIEW           (held for a dispatcher's confirmation)

Safety rules applied on top (they can only make the decision MORE cautious, never skip a dispatch):
  * the transparent rule score escalated the case (safety override)   -> at least DISPATCH_WITH_REVIEW
  * the model is unavailable                                           -> DISPATCH_WITH_REVIEW on the rule score
  * HUMAN_REVIEW for a case whose rule severity or any class with p >= LOW is CRITICAL is still held for review,
    but is auto-dispatched after HUMAN_REVIEW_TIMEOUT_S if nobody reviews it (see dispatch_service).
"""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


@dataclass(frozen=True)
class ConfidenceAssessment:
    confidence: float | None
    confidence_level: str          # HIGH / MEDIUM / LOW / UNAVAILABLE
    decision_mode: str             # AUTO_DISPATCH / DISPATCH_WITH_REVIEW / HUMAN_REVIEW
    reason: str
    runner_up: str | None = None
    margin: float | None = None    # top-1 minus top-2 probability

    def as_dict(self) -> dict:
        return {"confidence": self.confidence, "confidence_level": self.confidence_level,
                "decision_mode": self.decision_mode, "decision_reason": self.reason,
                "runner_up": self.runner_up, "margin": self.margin}


def assess(probabilities: dict[str, float] | None, predicted: str | None, high: float, low: float,
           safety_override: bool = False) -> ConfidenceAssessment:
    if not 0 <= low <= high <= 1:
        raise ValueError("require 0 <= DISPATCH_CONFIDENCE_LOW <= DISPATCH_CONFIDENCE_HIGH <= 1")
    if not probabilities or predicted is None:
        return ConfidenceAssessment(None, "UNAVAILABLE", "DISPATCH_WITH_REVIEW",
                                    "ML model unavailable - dispatching on the transparent rule score; review advised.")
    ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
    conf = float(probabilities.get(predicted, ranked[0][1]))
    runner = next((k for k, _ in ranked if k != predicted), None)
    margin = round(conf - (probabilities.get(runner, 0.0) if runner else 0.0), 4)
    alt = f" (runner-up {runner} {probabilities.get(runner, 0.0):.0%})" if runner else ""
    if conf >= high:
        level, mode = "HIGH", "AUTO_DISPATCH"
        reason = f"ML confidence {conf:.0%} >= automatic dispatch threshold {high:.0%}."
    elif conf >= low:
        level, mode = "MEDIUM", "DISPATCH_WITH_REVIEW"
        reason = (f"ML confidence {conf:.0%} is between {low:.0%} and {high:.0%}{alt}: dispatched immediately, "
                  f"flagged for dispatcher review.")
    else:
        level, mode = "LOW", "HUMAN_REVIEW"
        reason = (f"ML confidence {conf:.0%} is below the {low:.0%} threshold{alt}: a dispatcher must confirm "
                  f"the severity before automatic dispatch.")
    if safety_override and mode == "AUTO_DISPATCH":
        mode = "DISPATCH_WITH_REVIEW"
        reason += " Rule score escalated the severity (safety override): review advised."
    return ConfidenceAssessment(round(conf, 4), level, mode, reason, runner, margin)


def most_severe(*levels: str | None) -> str:
    present = [lv for lv in levels if lv in LEVELS]
    return max(present, key=LEVELS.index) if present else "MEDIUM"
