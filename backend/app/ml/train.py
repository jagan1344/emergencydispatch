"""Train and evaluate the severity classifier.

    python -m app.ml.train            (from the backend/ directory)

Pipeline: synthetic dataset -> preprocessing -> stratified 80/20 split -> {LogisticRegression,
RandomForest, GradientBoosting} -> evaluation -> RandomForest saved with joblib -> loaded by the API.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.ml.dataset import CATEGORICAL_FEATURES, CLASSES, FEATURES, NUMERIC_FEATURES, generate

ML_DIR = Path(__file__).resolve().parent
ARTIFACTS = ML_DIR / "artifacts"
MODEL_PATH = ARTIFACTS / "model.joblib"
METRICS_PATH = ARTIFACTS / "metrics.json"
DATA_PATH = ML_DIR / "data" / "synthetic_emergencies.csv"


def build(model_name: str, seed: int) -> Pipeline:
    scale = model_name == "logistic_regression"
    pre = ColumnTransformer([
        ("num", StandardScaler() if scale else "passthrough", NUMERIC_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
    ])
    if model_name == "logistic_regression":
        clf = LogisticRegression(max_iter=2000, random_state=seed)
    elif model_name == "random_forest":
        clf = RandomForestClassifier(n_estimators=200, max_depth=14, min_samples_leaf=2, class_weight="balanced",
                                     random_state=seed, n_jobs=-1)
    elif model_name == "gradient_boosting":
        clf = GradientBoostingClassifier(random_state=seed)
    else:
        raise ValueError(model_name)
    return Pipeline([("pre", pre), ("clf", clf)])


def evaluate(pipe: Pipeline, x_test, y_test) -> dict:
    pred = pipe.predict(x_test)
    p, r, f, _ = precision_recall_fscore_support(y_test, pred, labels=CLASSES, average="macro", zero_division=0)
    return {
        "accuracy": round(float(accuracy_score(y_test, pred)), 4),
        "precision_macro": round(float(p), 4), "recall_macro": round(float(r), 4), "f1_macro": round(float(f), 4),
        "confusion_matrix": {"labels": CLASSES, "matrix": confusion_matrix(y_test, pred, labels=CLASSES).tolist()},
        "per_class": classification_report(y_test, pred, labels=CLASSES, output_dict=True, zero_division=0),
    }


def main(n: int = 6000, seed: int = 42, quiet: bool = False) -> dict:
    df = generate(n=n, seed=seed)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DATA_PATH, index=False)
    x, y = df[FEATURES], df["severity"]
    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, stratify=y, random_state=seed)
    results = {}
    pipes = {}
    for name in ("logistic_regression", "random_forest", "gradient_boosting"):
        t0 = time.perf_counter()
        pipe = build(name, seed).fit(x_train, y_train)
        res = evaluate(pipe, x_test, y_test)
        res["train_seconds"] = round(time.perf_counter() - t0, 2)
        results[name] = res
        pipes[name] = pipe
        if not quiet:
            print(f"{name:22s} acc={res['accuracy']:.4f} P={res['precision_macro']:.4f} "
                  f"R={res['recall_macro']:.4f} F1={res['f1_macro']:.4f} ({res['train_seconds']}s)")
    deployed = "random_forest"
    rf: Pipeline = pipes[deployed]
    names = rf.named_steps["pre"].get_feature_names_out()
    importances = sorted(zip([n.split("__", 1)[1] for n in names], rf.named_steps["clf"].feature_importances_),
                         key=lambda t: -t[1])
    version = datetime.now(timezone.utc).strftime("rf-%Y%m%d%H%M%S")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": rf, "classes": CLASSES, "features": FEATURES, "version": version,
                 "model_name": "RandomForestClassifier"}, MODEL_PATH)
    report = {
        "disclaimer": "Synthetic, medically-inspired data for academic demonstration only. Not clinically validated.",
        "dataset": {"rows": len(df), "seed": seed, "class_counts": df["severity"].value_counts().to_dict(),
                    "train_rows": len(x_train), "test_rows": len(x_test), "split": "80/20 stratified"},
        "deployed_model": deployed, "version": version, "models": results,
        "feature_importances": [{"feature": f, "importance": round(float(v), 4)} for f, v in importances[:15]],
    }
    METRICS_PATH.write_text(json.dumps(report, indent=2))
    if not quiet:
        cm = np.array(results[deployed]["confusion_matrix"]["matrix"])
        print("RandomForest confusion matrix (rows=true, cols=pred)", CLASSES)
        print(cm)
        print(f"saved {MODEL_PATH} ({version})")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    main(a.rows, a.seed)
