"""Unified view of every automated decision taken for one incident, built only from stored data:
confidence assessment -> dispatch decision (+ explanation / counterfactuals) -> routes (current vs predicted
traffic) -> re-routes -> hospital decision (+ congestion predictions) -> resource conflicts -> decision trace."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.explain import explain_selection
from app.models import Dispatch, EmergencyIncident, ModelPrediction, ResourceConflict, Route, SystemEvent
from app.services.state import STATE


def _m(s) -> str:
    return "—" if s is None else f"{s / 60:.1f} min"


def trace_entry(e: SystemEvent) -> dict | None:
    d = e.payload or {}
    t = e.event_type
    title, detail = None, None
    if t == "EMERGENCY_CREATED":
        title = f"Emergency received ({d.get('emergency_type')}, source {d.get('source')})"
    elif t == "EMERGENCY_STATUS_CHANGED":
        title = f"Status {d.get('old_status')} → {d.get('status')}"
    elif t == "EMERGENCY_CLASSIFIED":
        title = (f"ML prediction {d.get('predicted_severity') or 'unavailable'}"
                 + (f" — {d['ml_confidence']:.0%} confidence" if d.get("ml_confidence") is not None else "")
                 + f"; rule score {d.get('rule_score')} ({d.get('rule_severity')}); final {d.get('severity')}")
    elif t == "CONFIDENCE_ASSESSED":
        title = {"AUTO_DISPATCH": "Automatic dispatch authorized", "DISPATCH_WITH_REVIEW": "Dispatch authorized with review flag",
                 "HUMAN_REVIEW": "Held for human review"}.get(d.get("decision_mode"), d.get("decision_mode"))
        detail = d.get("decision_reason")
    elif t in ("HUMAN_REVIEW_COMPLETED", "HUMAN_REVIEW_TIMEOUT"):
        title = ("Reviewed by " + str(d.get("reviewed_by")) + f" — severity {d.get('severity')}") if t.endswith("COMPLETED") \
            else f"No review in time — released with severity {d.get('severity')}"
    elif t == "DISPATCH_PENDING":
        title, detail = "Dispatch pending", d.get("reason")
    elif t == "DISPATCH_DECISION":
        title = f"{d.get('candidates')} ambulance candidates evaluated ({d.get('method')})"
        detail = d.get("summary")
    elif t == "DISPATCH_CREATED":
        title = f"{d.get('ambulance_id')} selected — score {d.get('score')}, ETA {_m(d.get('eta_s'))}"
        detail = f"route engine {d.get('engine')}"
    elif t == "ROUTE_SELECTED":
        title = f"{d.get('candidates')} route candidates evaluated — {d.get('engine')} route selected ({d.get('leg')})"
        detail = (f"ETA now {_m(d.get('eta_s'))}, with predicted traffic {_m(d.get('predicted_eta_s'))}"
                  f" (horizon {d.get('horizon_min')} min)")
    elif t == "ROUTE_DEGRADATION_DETECTED":
        title, detail = f"Route became suboptimal ({d.get('ambulance_id')})", d.get("reason")
    elif t == "ROUTE_CHECK":
        title = f"Alternative route evaluated — {d.get('decision')}"
        detail = d.get("detail") or d.get("reason")
    elif t == "ROUTE_RECALCULATED":
        title = f"Automatic reroute — saved {_m(d.get('time_saved_s'))}" if d.get("time_saved_s") is not None \
            else "Automatic reroute"
        detail = f"{d.get('reason')}; old ETA {_m(d.get('old_eta_s'))} → new ETA {_m(d.get('new_eta_s'))}"
    elif t == "AMBULANCE_STATUS_CHANGED":
        title = f"{d.get('ambulance_id')} {d.get('old_status')} → {d.get('status')}"
    elif t == "HOSPITAL_CONGESTION_PREDICTED":
        title = f"Hospital congestion predicted for {d.get('hospitals')} hospitals"
        detail = d.get("summary")
    elif t == "HOSPITAL_SELECTED":
        title, detail = f"{d.get('hospital_name')} selected", d.get("explanation")
    elif t == "HOSPITAL_WARNING":
        title = f"Hospital warning: {d.get('warning')}"
    elif t in ("RESOURCE_CONFLICT_DETECTED", "RESOURCE_REALLOCATED", "RESOURCE_ESCALATED"):
        title = {"RESOURCE_CONFLICT_DETECTED": "Resource conflict detected", "RESOURCE_REALLOCATED": "Ambulance reallocated",
                 "RESOURCE_ESCALATED": "Reallocation escalated to dispatcher"}[t] + f" ({d.get('ambulance_id')})"
        detail = d.get("reason")
    elif t == "AMBULANCE_AT_HOSPITAL":
        title = "Hospital reached"
    elif t == "INCIDENT_COMPLETED":
        title = f"Incident completed (response {_m(d.get('response_time_s'))})"
    if title is None:
        return None
    return {"at": e.created_at, "event": t, "title": title, "detail": detail}


def conflict_dict(c: ResourceConflict) -> dict:
    return {"id": str(c.id), "created_at": c.created_at, "ambulance_id": c.ambulance_id,
            "from_incident_id": str(c.from_incident_id) if c.from_incident_id else None,
            "to_incident_id": str(c.to_incident_id) if c.to_incident_id else None, "kind": c.kind,
            "decision": c.decision, "reason": c.reason, "from_eta_before_s": c.from_eta_before_s,
            "from_eta_after_s": c.from_eta_after_s, "to_eta_s": c.to_eta_s, "alternative_eta_s": c.alternative_eta_s,
            "impact_s": (c.from_eta_after_s - c.from_eta_before_s) if c.from_eta_after_s is not None
            and c.from_eta_before_s is not None else None, "details": c.details,
            "resolved_by": c.resolved_by, "resolved_at": c.resolved_at}


def build_decision(db: Session, inc: EmergencyIncident) -> dict:
    st = get_settings()
    pred = db.scalars(select(ModelPrediction).where(ModelPrediction.incident_id == inc.id)).first()
    out: dict = {"incident": {"id": str(inc.id), "reference": inc.reference, "status": inc.status,
                              "severity": inc.severity, "dispatch_note": inc.dispatch_note}}
    out["confidence"] = {
        "predicted_severity": inc.predicted_severity, "confidence": inc.ml_confidence,
        "class_probabilities": inc.class_probabilities, "confidence_level": inc.confidence_level,
        "decision_mode": inc.decision_mode, "decision_reason": inc.decision_reason,
        "rule_score": inc.rule_score, "rule_severity": inc.rule_severity, "final_severity": inc.severity,
        "model_version": pred.model_version if pred else STATE.model.version,
        "thresholds": {"high": st.dispatch_confidence_high, "low": st.dispatch_confidence_low},
        "reviewed_by": inc.reviewed_by, "reviewed_at": inc.reviewed_at}
    disp = db.scalars(select(Dispatch).where(Dispatch.incident_id == inc.id).order_by(Dispatch.created_at.desc())).first()
    if disp is None:
        out["dispatch"] = None
    else:
        xai = disp.counterfactuals or explain_selection(disp.candidates, disp.ambulance_id, disp.method)
        out["dispatch"] = {"ambulance_id": disp.ambulance_id, "method": disp.method, "decision_mode": disp.decision_mode,
                           "score": disp.dispatch_score, "eta_s": disp.eta_to_patient_s,
                           "distance_m": disp.distance_to_patient_m, "status": disp.status,
                           "candidates_considered": len(disp.candidates), "candidates": disp.candidates,
                           "explanation": xai, "objective": "minimise DispatchScore = Σ weight·component",
                           "constraints": ["status AVAILABLE, on duty, fuel ≥ 10 %",
                                           "capability match > 0 unless no suitable unit exists",
                                           "one ambulance per incident (OR-Tools when several incidents compete)"]}
    routes = db.scalars(select(Route).where(Route.incident_id == inc.id).order_by(Route.created_at)).all()
    out["routes"] = [{"id": str(r.id), "leg": r.leg, "engine": r.engine, "active": r.active, "created_at": r.created_at,
                      "distance_m": r.distance_m, "base_duration_s": r.base_duration_s,
                      "adjusted_duration_s": r.adjusted_duration_s, "predicted_duration_s": r.predicted_duration_s,
                      "prediction_horizon_min": r.prediction_horizon_min, "alternatives": r.alternatives,
                      "reroute_of": str(r.reroute_of) if r.reroute_of else None, "reroute_reason": r.reroute_reason,
                      "old_eta_s": r.old_eta_s, "time_saved_s": r.time_saved_s} for r in routes]
    out["reroutes"] = [r for r in out["routes"] if r["reroute_of"]]
    out["traffic"] = live_traffic_view(inc)
    out["hospital"] = None if disp is None or not disp.hospital_candidates else {
        "selected": disp.hospital_id, "candidates": disp.hospital_candidates, "explanation": disp.hospital_explanation}
    if out["hospital"] and disp.hospital_id:
        from app.dispatch.explain import explain_hospital
        try:
            out["hospital"]["counterfactual"] = explain_hospital(disp.hospital_candidates, disp.hospital_id)
        except (KeyError, StopIteration):
            out["hospital"]["counterfactual"] = None
    out["conflicts"] = [conflict_dict(c) for c in db.scalars(select(ResourceConflict).where(
        or_(ResourceConflict.from_incident_id == inc.id, ResourceConflict.to_incident_id == inc.id))
        .order_by(ResourceConflict.created_at))]
    events = db.scalars(select(SystemEvent).where(SystemEvent.incident_id == inc.id)
                        .order_by(SystemEvent.created_at, SystemEvent.id)).all()
    out["trace"] = [x for x in (trace_entry(e) for e in events) if x]
    from app.services.events import _jsonable
    return _jsonable(out)        # infinite ETAs (closed roads) -> null; JSON has no Infinity


def live_traffic_view(inc: EmergencyIncident) -> dict | None:
    """Current vs predicted remaining ETA on the incident's active route (filled by the prediction service)."""
    from app.services.routes_service import ACTIVE, predicted_remaining_eta, remaining_eta
    ar = ACTIVE.get(inc.assigned_ambulance) if inc.assigned_ambulance else None
    if ar is None or ar.incident_id != inc.id or STATE.graph is None:
        return None
    cur = remaining_eta(ar)
    prd = predicted_remaining_eta(ar)
    return {"route_id": str(ar.route_id), "leg": ar.leg, "current_eta_s": round(cur, 1),
            "predicted_eta_s": None if prd is None else round(prd, 1),
            "eta_impact_s": None if prd is None else round(prd - cur, 1),
            "horizon_min": get_settings().traffic_prediction_horizon_min,
            "roads_predicted_worse": predicted_worse_roads(ar)}


def predicted_worse_roads(ar) -> list[dict]:
    g = STATE.graph
    out = []
    seen = set()
    for s in ar.segments:
        r = s.road_id
        if not r or r in seen or r not in g.road_idx:
            continue
        seen.add(r)
        i = g.road_idx[r]
        if g.road_pred_speed_mps(i) < g.road_speed_mps(i) * 0.9:
            out.append({"road_id": r, "name": g.road_names[i], "current_level": g.road_level[i],
                        "predicted_level": g.road_pred_level[i]})
    return out[:10]
