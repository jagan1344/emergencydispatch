"""SYNTHETIC, medically-INSPIRED emergency dataset - for academic demonstration ONLY.

The data is generated from a latent "acuity" class per case and class-conditional distributions of
vital signs and observations, with deliberate overlap and label noise so the classification task is
non-trivial. It has NOT been derived from, or validated against, real patient data and the resulting
model must never be used for medical decisions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CLASSES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
EMERGENCY_TYPES = ["accident", "cardiac", "respiratory", "trauma", "fire", "stroke", "other"]
ACCIDENT_TYPES = ["NONE", "ROAD", "FALL", "FIRE", "INDUSTRIAL", "OTHER"]
CONSCIOUSNESS = ["ALERT", "VERBAL", "PAIN", "UNRESPONSIVE"]
GRADE = ["NONE", "MINOR", "MODERATE", "SEVERE"]

NUMERIC_FEATURES = ["age", "heart_rate", "respiratory_rate", "oxygen_saturation", "consciousness",
                    "bleeding", "injury_severity", "breathing_difficulty", "chest_pain"]
CATEGORICAL_FEATURES = ["emergency_type", "accident_type"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# prior over acuity class for each emergency type
TYPE_PRIOR = {
    "accident": [0.25, 0.35, 0.25, 0.15], "cardiac": [0.10, 0.25, 0.35, 0.30],
    "respiratory": [0.20, 0.35, 0.30, 0.15], "trauma": [0.20, 0.35, 0.28, 0.17],
    "fire": [0.25, 0.30, 0.25, 0.20], "stroke": [0.05, 0.20, 0.40, 0.35], "other": [0.50, 0.30, 0.15, 0.05],
}
TYPE_WEIGHTS = [0.24, 0.16, 0.14, 0.14, 0.06, 0.08, 0.18]
ACCIDENT_FOR_TYPE = {
    "accident": ["ROAD", "ROAD", "ROAD", "FALL", "INDUSTRIAL"], "trauma": ["FALL", "ROAD", "INDUSTRIAL", "OTHER"],
    "fire": ["FIRE"], "cardiac": ["NONE"], "respiratory": ["NONE"], "stroke": ["NONE"], "other": ["NONE", "OTHER"],
}


def _clip(v, lo, hi):
    return int(max(lo, min(hi, round(v))))


def generate(n: int = 6000, seed: int = 42, label_noise: float = 0.04) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        et = rng.choice(EMERGENCY_TYPES, p=np.array(TYPE_WEIGHTS) / sum(TYPE_WEIGHTS))
        k = int(rng.choice(4, p=TYPE_PRIOR[et]))           # latent acuity 0..3
        age = _clip(rng.normal(62 if et in ("cardiac", "stroke") else 40, 20), 1, 99)
        direction = rng.choice([-1, 1], p=[0.2, 0.8])        # tachycardia more common than bradycardia
        hr = _clip(82 + direction * ([4, 22, 40, 60][k] + abs(rng.normal(0, 9))), 25, 220)
        rr = _clip(15 + [1, 5, 9, 14][k] + rng.normal(0, 2.5), 4, 60)
        spo2 = _clip(98 - [0.5, 3, 6.5, 12][k] - abs(rng.normal(0, 1.8)), 60, 100)
        cons = int(min(3, rng.poisson([0.02, 0.15, 0.9, 2.2][k])))
        trauma_like = et in ("accident", "trauma", "fire")
        bleed = int(min(3, rng.poisson([0.3, 0.8, 1.5, 2.3][k] if trauma_like else [0.05, 0.1, 0.25, 0.4][k])))
        injury = int(min(3, rng.poisson([0.4, 1.0, 1.8, 2.6][k] if trauma_like else [0.05, 0.15, 0.3, 0.5][k])))
        breath = int(rng.random() < ([0.05, 0.2, 0.45, 0.7][k] + (0.25 if et in ("respiratory", "fire") else 0)))
        chest = int(rng.random() < ([0.05, 0.15, 0.3, 0.4][k] + (0.45 if et == "cardiac" else 0)))
        if rng.random() < label_noise:
            k = int(np.clip(k + rng.choice([-1, 1]), 0, 3))
        rows.append({
            "age": age, "heart_rate": hr, "respiratory_rate": rr, "oxygen_saturation": spo2,
            "consciousness": cons, "bleeding": bleed, "injury_severity": injury,
            "breathing_difficulty": breath, "chest_pain": chest,
            "emergency_type": et, "accident_type": str(rng.choice(ACCIDENT_FOR_TYPE[et])),
            "severity": CLASSES[k],
        })
    return pd.DataFrame(rows)


def encode_case(case: dict) -> dict:
    """Map API-level values (strings / bools) onto model features."""
    return {
        "age": int(case["patient_age"]),
        "heart_rate": int(case["heart_rate"]),
        "respiratory_rate": int(case["respiratory_rate"]),
        "oxygen_saturation": int(case.get("oxygen_saturation") or 97),
        "consciousness": CONSCIOUSNESS.index(case["consciousness"]),
        "bleeding": GRADE.index(case["bleeding"]),
        "injury_severity": GRADE.index(case["injury_severity"]),
        "breathing_difficulty": int(bool(case["breathing_difficulty"])),
        "chest_pain": int(bool(case.get("chest_pain", False))),
        "emergency_type": case["emergency_type"],
        "accident_type": case["accident_type"],
    }
