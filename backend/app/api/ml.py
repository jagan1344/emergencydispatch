from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import any_user
from app.dispatch.severity import combine_severity, severity_score
from app.ml.predict import ModelUnavailable
from app.models import User
from app.schemas.schemas import CaseFeatures
from app.services.state import STATE

router = APIRouter(prefix="/api/ml", tags=["ml"])


@router.get("/model")
def model_info(_: User = Depends(any_user)):
    return {"available": STATE.model.available, "version": STATE.model.version, "error": STATE.model.error,
            "metrics": STATE.model.metrics()}


@router.post("/predict")
def predict(body: CaseFeatures, _: User = Depends(any_user)):
    """ML severity + transparent rule score for a case (no incident is created)."""
    case = body.model_dump()
    rule = severity_score(case)
    try:
        pred = STATE.model.predict(case)
    except ModelUnavailable as exc:
        raise HTTPException(503, f"ML model unavailable: {exc}")
    final, basis = combine_severity(pred.severity, rule.level)
    return {"ml": {"severity": pred.severity, "confidence": pred.confidence, "probabilities": pred.probabilities,
                   "model_version": pred.model_version, "latency_ms": round(pred.latency_ms, 2)},
            "rule": {"score": rule.score, "severity": rule.level, "components": rule.components, "reasons": rule.reasons},
            "final_severity": final, "basis": basis}
