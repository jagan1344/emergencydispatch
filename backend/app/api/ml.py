from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import any_user
from app.config import get_settings
from app.dispatch.triage import triage
from app.models import User
from app.schemas.schemas import CaseFeatures
from app.services.state import STATE

router = APIRouter(prefix="/api/ml", tags=["ml"])


@router.get("/model")
def model_info(_: User = Depends(any_user)):
    return {"available": STATE.model.available, "version": STATE.model.version, "dataset": STATE.model.dataset,
            "error": STATE.model.error,
            "metrics": STATE.model.metrics()}


@router.post("/predict")
def predict(body: CaseFeatures, _: User = Depends(any_user)):
    """ML severity + transparent rule score for a case (no incident is created)."""
    case = body.model_dump()
    st = get_settings()
    t = triage(case, STATE.model, st.dispatch_confidence_high, st.dispatch_confidence_low)
    if t.prediction is None:
        raise HTTPException(503, f"ML model unavailable: {t.ml_error}")
    pred, rule = t.prediction, t.rule
    return {"decision": t.assessment.as_dict(),
            "ml": {"severity": pred.severity, "confidence": pred.confidence, "probabilities": pred.probabilities,
                   "model_version": pred.model_version, "latency_ms": round(pred.latency_ms, 2)},
            "rule": {"score": rule.score, "severity": rule.level, "components": rule.components, "reasons": rule.reasons},
            "final_severity": t.severity, "basis": t.basis}
