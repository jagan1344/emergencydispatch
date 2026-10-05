"""Transparent, rule-based emergency prioritisation score (an ENGINEERING heuristic, not a medical formula).

SeverityScore = 100 × ( 0.25·VitalRisk + 0.20·ConsciousnessRisk + 0.20·BreathingRisk
                      + 0.15·BleedingRisk + 0.10·InjuryRisk + 0.10·IncidentRisk )
Each component is normalised to 0..1.  0–25 LOW · 26–50 MEDIUM · 51–75 HIGH · 76–100 CRITICAL
"""
from __future__ import annotations

from dataclasses import dataclass

SEVERITY_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
WEIGHTS = {"vital": 0.25, "consciousness": 0.20, "breathing": 0.20, "bleeding": 0.15, "injury": 0.10,
           "incident": 0.10}
CONSCIOUSNESS_RISK = {"ALERT": 0.0, "VERBAL": 0.4, "PAIN": 0.75, "UNRESPONSIVE": 1.0}
GRADE_RISK = {"NONE": 0.0, "MINOR": 0.25, "MODERATE": 0.6, "SEVERE": 1.0}
INCIDENT_RISK = {"cardiac": 0.8, "stroke": 0.8, "fire": 0.7, "respiratory": 0.6, "trauma": 0.6,
                 "accident": 0.5, "other": 0.2}


def _band(value: float, lo_ok: float, hi_ok: float, lo_crit: float, hi_crit: float) -> float:
    """0 inside the normal band [lo_ok, hi_ok], rising linearly to 1 at lo_crit / hi_crit."""
    if value < lo_ok:
        return min(1.0, (lo_ok - value) / (lo_ok - lo_crit))
    if value > hi_ok:
        return min(1.0, (value - hi_ok) / (hi_crit - hi_ok))
    return 0.0


def hr_risk(hr: float) -> float:
    return _band(hr, 60, 100, 35, 160)


def rr_risk(rr: float) -> float:
    return _band(rr, 12, 20, 6, 36)


def spo2_risk(spo2: float | None) -> float:
    if spo2 is None:
        return 0.0
    return _band(spo2, 95, 101, 82, 102)


def age_risk(age: float) -> float:
    if age >= 75 or age < 2:
        return 1.0
    if age >= 65 or age < 6:
        return 0.5
    return 0.0


def level_for_score(score: float) -> str:
    if score <= 25:
        return "LOW"
    if score <= 50:
        return "MEDIUM"
    if score <= 75:
        return "HIGH"
    return "CRITICAL"


@dataclass
class SeverityResult:
    score: float
    level: str
    components: dict[str, float]
    reasons: list[str]


def severity_score(case: dict) -> SeverityResult:
    hr, rr = case["heart_rate"], case["respiratory_rate"]
    spo2 = case.get("oxygen_saturation")
    h, r, s, a = hr_risk(hr), rr_risk(rr), spo2_risk(spo2), age_risk(case["patient_age"])
    vital = min(1.0, 0.35 * h + 0.25 * r + 0.30 * s + 0.10 * a + 0.4 * max(h, r, s) * (max(h, r, s) > 0.8))
    consciousness = CONSCIOUSNESS_RISK[case["consciousness"]]
    breathing = min(1.0, 0.7 * bool(case["breathing_difficulty"]) + 0.3 * r + 0.4 * s)
    bleeding = GRADE_RISK[case["bleeding"]]
    injury = GRADE_RISK[case["injury_severity"]]
    incident = INCIDENT_RISK[case["emergency_type"]]
    if case["emergency_type"] == "cardiac" and case.get("chest_pain"):
        incident = 1.0
    if case.get("accident_type") in ("ROAD", "INDUSTRIAL", "FIRE"):
        incident = min(1.0, incident + 0.2)
    comps = {"vital": vital, "consciousness": consciousness, "breathing": breathing, "bleeding": bleeding,
             "injury": injury, "incident": incident}
    score = round(100 * sum(WEIGHTS[k] * v for k, v in comps.items()), 1)

    reasons: list[str] = []
    if h > 0:
        reasons.append(f"abnormal heart rate ({hr} bpm)")
    if r > 0:
        reasons.append(f"abnormal respiratory rate ({rr}/min)")
    if s > 0:
        reasons.append(f"low oxygen saturation ({spo2}%)")
    if case["breathing_difficulty"]:
        reasons.append("breathing difficulty")
    if consciousness >= 0.75:
        reasons.append("loss of consciousness" if consciousness == 1.0 else "responds only to pain")
    elif consciousness > 0:
        reasons.append("reduced consciousness (verbal response only)")
    if bleeding >= 0.6:
        reasons.append(f"{case['bleeding'].lower()} bleeding")
    if injury >= 0.6:
        reasons.append(f"{case['injury_severity'].lower()} injury")
    if case.get("chest_pain"):
        reasons.append("chest pain")
    if a >= 0.5:
        reasons.append(f"high-risk age group ({case['patient_age']} y)")
    if case["emergency_type"] in ("cardiac", "stroke"):
        reasons.append(f"time-critical {case['emergency_type']} emergency")
    return SeverityResult(score, level_for_score(score), {k: round(v, 3) for k, v in comps.items()}, reasons)


def combine_severity(ml_level: str | None, rule_level: str) -> tuple[str, str]:
    """Final severity used for dispatch.

    The ML prediction is primary. If the transparent rule score is two or more levels higher, the case
    is escalated to the rule level (safety override: under-triage is costlier than over-triage).
    Without a model the rule level is used and this is reported, never hidden.
    """
    if ml_level is None:
        return rule_level, "RULE_ONLY (ML model unavailable)"
    mi, ri = SEVERITY_LEVELS.index(ml_level), SEVERITY_LEVELS.index(rule_level)
    if ri - mi >= 2:
        return rule_level, f"SAFETY_OVERRIDE (rule {rule_level} ≥ 2 levels above ML {ml_level})"
    return ml_level, "ML"


def required_capability(severity: str, emergency_type: str) -> str:
    if severity == "CRITICAL":
        return "ICU"
    if severity == "HIGH" or emergency_type in ("cardiac", "stroke") and severity == "MEDIUM":
        return "ADVANCED"
    return "BASIC"
