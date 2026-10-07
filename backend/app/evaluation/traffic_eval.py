"""Offline traffic-prediction evaluation on the stored traffic history (the same data the live predictor uses):
ML (RandomForest) vs transparent fallback rules vs persistence, on a time-ordered hold-out.

    python -m app.evaluation.traffic_eval            -> evaluation/traffic_prediction.json

The traffic history in this project is SIMULATED (traffic simulator + dispatcher events); results describe that
history, not real Bengaluru traffic. ML is NOT assumed to be better: the comparison is reported as measured, and the
live system itself only uses the model when it beats persistence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.config import PROJECT_DIR

OUT = PROJECT_DIR / "evaluation" / "traffic_prediction.json"
log = logging.getLogger("app.evaluation")


def evaluate() -> dict:
    from app.config import get_settings
    from app.services.traffic_prediction import TrafficPredictor
    from app.utils.logging import log_event
    pred = TrafficPredictor()
    model = pred.retrain()
    m = dict(model.metrics)
    st = get_settings()
    measurable = "accuracy_model" in m
    out = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "source": "SIMULATION",
           "history_events": pred.status()["history_events"], "horizon_min": st.traffic_prediction_horizon_min,
           "samples": m.get("samples"), "holdout_samples": m.get("holdout"),
           "deployed_method": "LEARNED" if model.method == "MODEL" else "FALLBACK", "model_version": model.version,
           "reason": m.get("reason"),
           "comparison": None if not measurable else {
               "ml": {"accuracy": m["accuracy_model"], "mae_levels": m["mae_model"]},
               "fallback": {"accuracy": m["accuracy_fallback"], "mae_levels": m["mae_fallback"]},
               "persistence": {"accuracy": m["accuracy_persistence"], "mae_levels": m["mae_persistence"]},
               "best_by_accuracy": max((("ml", m["accuracy_model"]), ("fallback", m["accuracy_fallback"]),
                                        ("persistence", m["accuracy_persistence"])), key=lambda kv: kv[1])[0]},
           "status": "EVALUATED" if measurable else "INSUFFICIENT DATA",
           "note": "time-ordered hold-out (last 25 % of samples); levels FREE..BLOCKED as 0..5"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    log_event(log, "TRAFFIC_PREDICTION_EVALUATED", status=out["status"], deployed=out["deployed_method"],
              samples=out["samples"], best=(out["comparison"] or {}).get("best_by_accuracy"))
    return out


if __name__ == "__main__":
    from app.database import init_engine
    from app.utils.logging import configure_logging
    configure_logging()
    init_engine()
    res = evaluate()
    print(json.dumps({k: res[k] for k in ("status", "source", "deployed_method", "samples", "holdout_samples",
                                          "comparison")}, indent=2))
    print(f"wrote {OUT}")
