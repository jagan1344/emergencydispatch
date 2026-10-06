"""Dispatch engine: candidate search (PostGIS) → traffic-aware routes → DispatchScore → assignment."""
from __future__ import annotations

import logging
import math
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.explain import explain_selection
from app.dispatch.optimizer import optimal_assignment
from app.dispatch.priority import IncidentPriorityQueue
from app.dispatch.scoring import CandidateInput, ScoredCandidate, explain_dispatch, score_candidates
from app.models import Ambulance, Dispatch, EmergencyIncident
from app.routing.engine import NoRouteError, RouteResult
from app.services import metrics
from app.services.events import emit
from app.services.incident_service import refresh_waiting_priorities, set_status
from app.services.routes_service import activate, emit_route_selected, save_route
from app.services.state import STATE, require_router
from app.utils.logging import log_event
from app.utils.timeutil import sim_seconds, utcnow

log = logging.getLogger("app.dispatch")
EQUIPMENT_AT_LEAST = {"BASIC": ("BASIC", "ADVANCED", "ICU"), "ADVANCED": ("ADVANCED", "ICU"), "ICU": ("ICU",)}


class NoAmbulanceAvailable(RuntimeError):
    pass


def candidate_ids(db: Session, inc: EmergencyIncident, k: int) -> list[tuple[str, float]]:
    """Nearest AVAILABLE units (PostGIS KNN on geography) plus the 2 nearest units that meet the required
    capability, so a better-equipped unit slightly further away is always evaluated."""
    params = {"id": inc.id, "k": k}
    base = ("SELECT a.id, ST_Distance(a.location, i.location) FROM ambulances a, emergency_incidents i "
            "WHERE i.id = :id AND a.status = 'AVAILABLE' AND a.driver_status = 'ON_DUTY' AND a.fuel_level >= 10 ")
    rows = db.execute(text(base + "ORDER BY a.location <-> i.location LIMIT :k"), params).all()
    caps = EQUIPMENT_AT_LEAST[inc.required_capability or "BASIC"]
    rows += db.execute(text(base + "AND a.equipment_level = ANY(:caps) ORDER BY a.location <-> i.location LIMIT 2"),
                       {**params, "caps": list(caps)}).all()
    seen, out = set(), []
    for rid, d in rows:
        if rid not in seen:
            seen.add(rid)
            out.append((rid, float(d)))
    return out


def evaluate_candidates(db: Session, inc: EmergencyIncident, k: int | None = None
                        ) -> tuple[list[ScoredCandidate], dict[str, RouteResult]]:
    router = require_router()
    k = k or get_settings().max_dispatch_candidates
    ids = candidate_ids(db, inc, k)
    if not ids:
        raise NoAmbulanceAvailable("No available ambulance: every unit is busy, off duty, low on fuel or offline")
    ambs = {a.id: a for a in db.scalars(select(Ambulance).where(Ambulance.id.in_([i for i, _ in ids])))}
    inputs, routes = [], {}
    for amb_id, straight in ids:
        a = ambs[amb_id]
        try:
            rr = router.route((a.latitude, a.longitude), (inc.latitude, inc.longitude), compute_shortest=False)
        except NoRouteError as exc:
            log_event(log, "CANDIDATE_UNREACHABLE", ambulance_id=amb_id, incident_id=str(inc.id), error=str(exc))
            continue
        routes[amb_id] = rr
        inputs.append(CandidateInput(amb_id, a.equipment_level, rr.eta_s, rr.distance_m,
                                     max(0.0, rr.eta_s - rr.base_duration_s), a.missions_today, a.fuel_level,
                                     extra={"straight_line_m": round(straight, 1), "engine": rr.engine,
                                            "base_duration_s": round(rr.base_duration_s, 1),
                                            "current_eta_s": round(rr.adjusted_duration_s, 1),
                                            "fuel_level": round(a.fuel_level, 1), "missions_today": a.missions_today}))
    if not inputs:
        raise NoAmbulanceAvailable("No available ambulance can reach the incident on the current road network")
    ranked = score_candidates(inputs, inc.required_capability or "BASIC")
    log_event(log, "AMBULANCE_CANDIDATES", incident_id=str(inc.id),
              candidates=[{"id": c.ambulance_id, "score": round(c.score, 3), "eta_s": round(c.eta_s)} for c in ranked])
    return ranked, routes


def _dispatch(db: Session, inc: EmergencyIncident, chosen: ScoredCandidate, rr: RouteResult, ranked: list[ScoredCandidate],
              method: str, decision_ms: float, note: str | None = None, *, reallocated_from=None,
              origin: tuple[float, float] | None = None) -> Dispatch:
    """reallocated_from: the ActiveRoute of a unit diverted from another incident (see services.reallocation);
    the unit is then busy, starts from `origin` (its current position) and the simulator receives ROUTE_UPDATED."""
    amb = db.get(Ambulance, chosen.ambulance_id, with_for_update=True)
    if amb is None or (amb.status != "AVAILABLE" and reallocated_from is None):
        raise NoAmbulanceAvailable(f"{chosen.ambulance_id} is no longer available")
    order = [chosen] + [c for c in ranked if c.ambulance_id != chosen.ambulance_id]
    explanation = explain_dispatch(order)
    if not chosen.suitable:
        explanation += "\nWARNING: no unit with the required capability was available."
    if rr.through_closure:
        explanation += (f"\nWARNING: the incident can only be reached through closed road(s) "
                        f"{', '.join(rr.through_closure)} - access at walking pace assumed; arrange police escort.")
    if note:
        explanation += "\n" + note
    start = origin or (amb.latitude, amb.longitude)
    if rr.shortest_distance_m is None:
        rr.shortest_distance_m = require_router().shortest_distance(start, (inc.latitude, inc.longitude))
    cand_dicts = [c.as_dict() for c in ranked]
    xai = explain_selection(cand_dicts, amb.id, method)
    dispatch = Dispatch(id=uuid.uuid4(), incident_id=inc.id, ambulance_id=amb.id, method=method,
                        decision_mode=inc.decision_mode, counterfactuals=xai,
                        dispatch_score=chosen.score, eta_to_patient_s=chosen.eta_s, distance_to_patient_m=chosen.distance_m,
                        candidates=cand_dicts, explanation=explanation, decision_ms=decision_ms)
    db.add(dispatch)
    db.flush()
    dest = (inc.latitude, inc.longitude)
    route = save_route(db, rr, leg="TO_PATIENT", origin=start, dest=dest, ambulance_id=amb.id, incident_id=inc.id,
                       dispatch_id=dispatch.id)
    # decision first, then its consequences (keeps the decision trace in causal order)
    emit(db, "DISPATCH_DECISION", {
        "incident_id": str(inc.id), "ambulance_id": amb.id, "method": method, "decision_mode": inc.decision_mode,
        "candidates": len(ranked), "score": round(chosen.score, 4), "summary": xai["summary"],
        "runner_up": xai["runner_up"]}, incident_id=inc.id, ambulance_id=amb.id)
    emit_route_selected(db, rr, "TO_PATIENT", inc.id, amb.id, route.id)
    now = utcnow()
    inc.assigned_ambulance = amb.id
    inc.dispatch_note = None
    inc.dispatched_at = now
    set_status(db, inc, "DISPATCHED", ambulance_id=amb.id)
    old_status = amb.status
    amb.status = "DISPATCHED"
    amb.current_incident = inc.id
    amb.destination = inc.reference
    amb.destination_lat, amb.destination_lon = dest
    amb.last_updated = now
    emit(db, "AMBULANCE_STATUS_CHANGED", {"ambulance_id": amb.id, "old_status": old_status, "status": amb.status,
                                          "incident_id": str(inc.id)}, incident_id=inc.id, ambulance_id=amb.id)
    emit(db, "DISPATCH_CREATED", {
        "dispatch_id": str(dispatch.id), "incident_id": str(inc.id), "reference": inc.reference,
        "ambulance_id": amb.id, "score": round(chosen.score, 4), "eta_s": round(chosen.eta_s, 1),
        "distance_m": round(chosen.distance_m, 1), "method": method, "explanation": explanation,
        "candidates": [c.as_dict() for c in ranked], "route_id": str(route.id), "engine": rr.engine,
        "geometry": [[round(a, 6), round(b, 6)] for a, b in rr.coords], "decision_ms": round(decision_ms, 1),
    }, incident_id=inc.id, ambulance_id=amb.id)
    activate(db, route, rr, amb.id, inc.id, "TO_PATIENT", dest, previous=reallocated_from)
    log_event(log, "DISPATCH_EXPLANATION_GENERATED", incident_id=str(inc.id), ambulance_id=amb.id,
              reasons=xai["reasons"], runner_up=xai["runner_up"])
    log_event(log, "COUNTERFACTUAL_EVALUATED", incident_id=str(inc.id), ambulance_id=amb.id,
              alternatives=[{"id": c["ambulance_id"], "eta_delta_s": c["eta_delta_s"], "score_delta": c["score_delta"]}
                            for c in xai["counterfactuals"]])
    metrics.DISPATCHES.labels(method).inc()
    metrics.DECISION_LATENCY.observe(decision_ms)
    dt = sim_seconds(inc.created_at, now)
    if dt is not None:
        metrics.DISPATCH_TIME.observe(dt)
    log_event(log, "DISPATCH_CREATED", incident_id=str(inc.id), ambulance_id=amb.id, score=round(chosen.score, 4),
              eta_s=round(chosen.eta_s, 1), method=method, decision_ms=round(decision_ms, 1))
    return dispatch


def dispatch_incident(db: Session, inc: EmergencyIncident, ambulance_id: str | None = None,
                      by: str | None = None) -> Dispatch:
    """Dispatch one incident now (manual dispatch or auto-dispatch of a single waiting incident)."""
    if inc.status != "WAITING":
        raise ValueError(f"incident is {inc.status}; only WAITING incidents can be dispatched")
    if inc.decision_mode == "HUMAN_REVIEW" and by:
        from app.services.incident_service import review_incident
        review_incident(db, inc, by)          # pressing "Dispatch now" is an explicit human authorisation
    t0 = time.perf_counter()
    ranked, routes = evaluate_candidates(db, inc)
    note = None
    if ambulance_id:
        pick = next((c for c in ranked if c.ambulance_id == ambulance_id), None)
        if pick is None:
            raise NoAmbulanceAvailable(f"{ambulance_id} is not an available candidate for this incident")
        if pick is not ranked[0]:
            note = f"MANUAL OVERRIDE by {by or 'dispatcher'}: system recommendation was {ranked[0].ambulance_id}."
    else:
        pick = ranked[0]
    return _dispatch(db, inc, pick, routes[pick.ambulance_id], ranked, "MANUAL" if ambulance_id else "WEIGHTED_SCORE",
                     (time.perf_counter() - t0) * 1000, note)


def dispatcher_cycle() -> list[str]:
    """Priority-queue driven auto-dispatch. Called after new incidents, after units become available and
    periodically. Uses OR-Tools when several incidents compete for the available units."""
    from app.database import session_scope

    dispatched: list[str] = []
    if STATE.router is None:
        return dispatched
    with STATE.lock, session_scope() as db:
        waiting = [i for i in refresh_waiting_priorities(db) if _authorised(db, i)]
        if not waiting:
            return dispatched
        available = db.scalar(text("SELECT count(*) FROM ambulances WHERE status='AVAILABLE' "
                                   "AND driver_status='ON_DUTY' AND fuel_level >= 10")) or 0
        if available == 0:
            from app.services.reallocation import consider_reallocation
            top = max(waiting, key=lambda i: (i.priority or 0))
            if consider_reallocation(db, top, None):
                dispatched.append(str(top.id))
                return dispatched
            from app.services.simulation_service import simulator_connected
            busy = db.scalar(text("SELECT count(*) FROM ambulances WHERE status NOT IN "
                                  "('AVAILABLE','OFFLINE','MAINTENANCE')")) or 0
            reason = f"no ambulance available - all {busy} on-duty units are on missions"
            if not simulator_connected():
                reason += ("; the ambulance simulator is NOT running, so dispatched units never finish their "
                           "missions - start it with: python simulator/run_simulator.py")
            for inc in waiting:
                if not (inc.dispatch_note or "").startswith("reallocation of"):   # keep pending escalations visible
                    _note(db, inc, reason)
            return dispatched
        pq = IncidentPriorityQueue()
        by_id = {str(i.id): i for i in waiting}
        for i in waiting:
            pq.push(str(i.id), i.priority or 0, i.created_at.timestamp())
        batch = []
        while len(batch) < min(5, len(by_id)):
            item = pq.pop()
            if item is None:
                break
            batch.append(by_id[item[0]])
        t0 = time.perf_counter()
        evals: dict[str, tuple[list[ScoredCandidate], dict[str, RouteResult]]] = {}
        for inc in batch:
            try:
                evals[str(inc.id)] = evaluate_candidates(db, inc, k=6 if len(batch) > 1 else None)
            except Exception as exc:  # one bad incident must never block the queue
                reason = str(exc) if isinstance(exc, NoAmbulanceAvailable) else f"dispatch error: {type(exc).__name__}: {exc}"
                if not isinstance(exc, NoAmbulanceAvailable):
                    log.exception("candidate evaluation failed", extra={"event": "DISPATCH_EVALUATION_FAILED",
                                                                         "fields": {"incident_id": str(inc.id)}})
                _note(db, inc, reason)
        # (5) resource reallocation: the highest-priority incident whose best free suitable unit is missing or too far
        from app.services.reallocation import consider_reallocation
        for inc in batch:
            ev = evals.get(str(inc.id))
            if consider_reallocation(db, inc, ev[0] if ev else None):
                dispatched.append(str(inc.id))
                return dispatched          # one reallocation per cycle; remaining incidents next cycle (3 s)
        if not evals:
            return dispatched
        if len(evals) == 1:
            iid, (ranked, routes) = next(iter(evals.items()))
            assignment = {iid: ranked[0].ambulance_id}
            method = "WEIGHTED_SCORE"
        else:
            assignment = optimal_assignment(
                {iid: by_id[iid].priority or 0 for iid in evals},
                {iid: {c.ambulance_id: (c.score, c.suitable) for c in ranked} for iid, (ranked, _) in evals.items()})
            method = "ORTOOLS_ASSIGNMENT"
        decision_ms = (time.perf_counter() - t0) * 1000
        for iid, amb_id in sorted(assignment.items(), key=lambda kv: -(by_id[kv[0]].priority or 0)):
            ranked, routes = evals[iid]
            chosen = next(c for c in ranked if c.ambulance_id == amb_id)
            note = None
            if method == "ORTOOLS_ASSIGNMENT" and chosen is not ranked[0]:
                note = (f"Global optimisation (OR-Tools) assigned {amb_id} instead of the individually best "
                        f"{ranked[0].ambulance_id}, which serves a higher-priority or better-matched incident.")
                holder = next((h for h, a in assignment.items() if a == ranked[0].ambulance_id), None)
                if holder is not None:
                    from app.services.reallocation import record_contested_unit
                    record_contested_unit(db, ranked[0], chosen, by_id[holder], by_id[iid])
            try:
                with db.begin_nested():
                    _dispatch(db, by_id[iid], chosen, routes[amb_id], ranked, method, decision_ms, note)
                dispatched.append(iid)
            except Exception as exc:
                log_event(log, "DISPATCH_SKIPPED", incident_id=iid, reason=str(exc))
                _note(db, by_id[iid], f"dispatch skipped: {exc}")
        for iid in evals:                       # evaluated but not assigned (more incidents than free units)
            if iid not in assignment:
                _note(db, by_id[iid], "all suitable ambulances were assigned to higher-priority incidents; waiting in queue")
    return dispatched


def _authorised(db: Session, inc: EmergencyIncident) -> bool:
    """HUMAN_REVIEW incidents wait for a dispatcher; after HUMAN_REVIEW_TIMEOUT_S (simulated) they are released
    with the more severe of the ML and rule severities, so that an unattended console never strands a patient."""
    if inc.decision_mode != "HUMAN_REVIEW":
        return True
    st = get_settings()
    waited = sim_seconds(inc.created_at, utcnow()) or 0.0
    if st.human_review_timeout_s > 0 and waited >= st.human_review_timeout_s:
        from app.dispatch.confidence import most_severe
        from app.dispatch.severity import required_capability
        sev = most_severe(inc.predicted_severity, inc.rule_severity, inc.severity)
        inc.severity, inc.required_capability = sev, required_capability(sev, inc.emergency_type)
        inc.decision_mode = "DISPATCH_WITH_REVIEW"
        inc.decision_reason = (f"No dispatcher review within {st.human_review_timeout_s:.0f} s: released for dispatch "
                               f"with the more severe of ML/rule severity ({sev}).")
        emit(db, "HUMAN_REVIEW_TIMEOUT", {"incident_id": str(inc.id), "reference": inc.reference, "severity": sev,
                                          "waited_s": round(waited, 1)}, incident_id=inc.id)
        return True
    _note(db, inc, f"awaiting human review: {inc.decision_reason}")
    return False


def _note(db: Session, inc: EmergencyIncident, reason: str) -> None:
    """Remember (and broadcast) why an incident is still waiting, so the dispatcher can act on it."""
    reason = reason[:500]
    if inc.dispatch_note != reason:
        inc.dispatch_note = reason
        emit(db, "DISPATCH_PENDING", {"incident_id": str(inc.id), "reference": inc.reference, "reason": reason},
             incident_id=inc.id)
        log_event(log, "DISPATCH_PENDING", incident_id=str(inc.id), reason=reason)


def preview(db: Session, inc: EmergencyIncident) -> dict:
    ranked, routes = evaluate_candidates(db, inc)
    best = ranked[0]
    return {"recommended": best.ambulance_id, "explanation": explain_dispatch(ranked),
            "candidates": [dict(c.as_dict(), route=routes[c.ambulance_id].summary()) for c in ranked]}


def eta_minutes(seconds: float) -> float | None:
    return None if seconds is None or math.isinf(seconds) else round(seconds / 60, 2)
