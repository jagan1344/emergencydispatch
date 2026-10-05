"""Loaders for REAL, publicly available emergency-department triage datasets.

1. KTAS dataset (recommended, no registration besides a free Kaggle account)
   Moon S-H, Shim JL, Park K-S, Park C-S (2019) "Triage accuracy and causes of mistriage using the Korean
   Triage and Acuity Scale". PLOS ONE 14(9): e0216972. 1 267 adult ED visits, CC BY 4.0.
   Kaggle: "Emergency Service - Triage Application" (file data.csv, ';'-separated, decimal comma).
   Columns used: Age, Injury (1 no / 2 yes), Chief_complain, Mental (1 alert, 2 verbal, 3 pain,
   4 unresponsive), SBP, HR, RR, BT, Saturation, KTAS_expert (1 most urgent ... 5 least urgent).

2. MIMIC-IV-ED (credentialed but free: https://physionet.org/content/mimic-iv-ed/)
   triage.csv: temperature (°F), heartrate, resprate, o2sat, sbp, acuity (ESI 1-5), chiefcomplaint.

Both use a 5-level acuity scale which is mapped onto the dispatcher's 4 severity classes:
    1 -> CRITICAL, 2 -> HIGH, 3 -> MEDIUM, 4/5 -> LOW
Missing measurements are kept as NaN and imputed (median) inside the model pipeline.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

ACUITY_TO_SEVERITY = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW", 5: "LOW"}
REAL_FEATURES = ["age", "heart_rate", "respiratory_rate", "oxygen_saturation", "systolic_bp", "temperature_c",
                 "consciousness", "injury", "chest_pain", "breathing_difficulty"]
CHEST_RE = re.compile(r"chest\s*(pain|discomfort|tight)|angina|\bcp\b", re.I)
BREATH_RE = re.compile(r"dyspn|short(ness)? of breath|\bsob\b|breath|respiratory distress|wheez", re.I)


def _num(s: pd.Series) -> pd.Series:
    """Numbers with decimal commas and junk markers ('??', '#BOÞ!', '') -> float/NaN."""
    return pd.to_numeric(s.astype(str).str.replace(",", ".", regex=False).str.strip(), errors="coerce")


def _clip(s: pd.Series, lo: float, hi: float) -> pd.Series:
    return s.where((s >= lo) & (s <= hi))  # physiologically impossible values -> NaN


def _read_any(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, sep=None, engine="python", encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"cannot decode {path}")


def load_ktas(path: str | Path) -> pd.DataFrame:
    df = _read_any(Path(path))
    df.columns = [c.strip() for c in df.columns]
    need = {"Age", "Mental", "HR", "RR", "KTAS_expert"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"not a KTAS data.csv - missing columns {sorted(missing)}")
    complaint = df.get("Chief_complain", pd.Series([""] * len(df))).fillna("").astype(str)
    out = pd.DataFrame({
        "age": _clip(_num(df["Age"]), 0, 120),
        "heart_rate": _clip(_num(df["HR"]), 20, 250),
        "respiratory_rate": _clip(_num(df["RR"]), 4, 70),
        "oxygen_saturation": _clip(_num(df["Saturation"]) if "Saturation" in df else np.nan, 50, 100),
        "systolic_bp": _clip(_num(df["SBP"]) if "SBP" in df else np.nan, 40, 280),
        "temperature_c": _clip(_num(df["BT"]) if "BT" in df else np.nan, 30, 43),
        "consciousness": _clip(_num(df["Mental"]) - 1, 0, 3),
        "injury": (_num(df["Injury"]) == 2).astype(int) if "Injury" in df else 0,
        "chest_pain": complaint.str.contains(CHEST_RE).astype(int),
        "breathing_difficulty": complaint.str.contains(BREATH_RE).astype(int),
        "acuity": _num(df["KTAS_expert"]),
    })
    return _finish(out)


def load_mimic_ed(path: str | Path) -> pd.DataFrame:
    df = _read_any(Path(path))
    need = {"heartrate", "resprate", "o2sat", "acuity"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"not a MIMIC-IV-ED triage.csv - missing columns {sorted(missing)}")
    complaint = df.get("chiefcomplaint", pd.Series([""] * len(df))).fillna("").astype(str)
    temp_f = _num(df["temperature"]) if "temperature" in df else pd.Series(np.nan, index=df.index)
    out = pd.DataFrame({
        "age": _clip(_num(df["anchor_age"]), 0, 120) if "anchor_age" in df else np.nan,
        "heart_rate": _clip(_num(df["heartrate"]), 20, 250),
        "respiratory_rate": _clip(_num(df["resprate"]), 4, 70),
        "oxygen_saturation": _clip(_num(df["o2sat"]), 50, 100),
        "systolic_bp": _clip(_num(df["sbp"]), 40, 280) if "sbp" in df else np.nan,
        "temperature_c": _clip((temp_f - 32) * 5 / 9, 30, 43),
        "consciousness": np.nan,          # not recorded at MIMIC triage -> imputed
        "injury": complaint.str.contains(r"fall|mvc|trauma|injur|laceration|fracture", case=False).astype(int),
        "chest_pain": complaint.str.contains(CHEST_RE).astype(int),
        "breathing_difficulty": complaint.str.contains(BREATH_RE).astype(int),
        "acuity": _num(df["acuity"]),
    })
    return _finish(out)


def _finish(out: pd.DataFrame) -> pd.DataFrame:
    out = out[out["acuity"].isin([1, 2, 3, 4, 5])].copy()
    out["severity"] = out["acuity"].astype(int).map(ACUITY_TO_SEVERITY)
    return out.drop(columns=["acuity"]).reset_index(drop=True)


LOADERS = {"ktas": load_ktas, "mimic-ed": load_mimic_ed}


def encode_case_real(case: dict) -> dict:
    """Map an intake case onto REAL_FEATURES (values the dispatcher does not provide stay NaN)."""
    from app.ml.dataset import CONSCIOUSNESS
    injured = (case.get("injury_severity", "NONE") != "NONE" or case.get("bleeding", "NONE") != "NONE"
               or case.get("emergency_type") in ("accident", "trauma"))
    return {
        "age": case.get("patient_age"), "heart_rate": case.get("heart_rate"),
        "respiratory_rate": case.get("respiratory_rate"),
        "oxygen_saturation": case.get("oxygen_saturation") if case.get("oxygen_saturation") is not None else np.nan,
        "systolic_bp": case.get("systolic_bp") if case.get("systolic_bp") is not None else np.nan,
        "temperature_c": case.get("temperature_c") if case.get("temperature_c") is not None else np.nan,
        "consciousness": CONSCIOUSNESS.index(case["consciousness"]),
        "injury": int(injured), "chest_pain": int(bool(case.get("chest_pain"))),
        "breathing_difficulty": int(bool(case.get("breathing_difficulty"))),
    }
