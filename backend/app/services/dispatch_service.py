"""Dispatch engine: candidate search (PostGIS) → traffic-aware routes → DispatchScore → assignment."""
from __future__ import annotations

import logging
import math
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.optimizer import optimal_assignment
from app.dispatch.priority import IncidentPriorityQueue
from app.dispatch.scoring import CandidateInput, ScoredCandidate, explain_dispatch, score_candidates
from app.models import Ambulance, Dispatch, EmergencyIncident
from app.routing.engine import NoRouteError, RouteResult
from app.services import metrics
from app.services.events import emit
from app.services.incident_service import refresh_waiting_priorities, set_status
from app.services.routes_service import activate, save_route
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
        inputs.append(CandidateInput(amb_id, a.equipment_level, rr.adjusted_duration_s, rr.distance_m,
                                     rr.traffic_delay_s, a.missions_today, a.fuel_level,
                                     extra={"straight_line_m": round(straight, 1), "engine": rr.engine,
                                            "base_duration_s": round(rr.base_duration_s, 1),
                                            "fuel_level": round(a.fuel_level, 1), "missions_today": a.missions_today}))
    if not inputs:
        raise NoAmbulanceAvailable("No available ambulance can reach the incident on the current road network")
    ranked = score_candidates(inputs, inc.required_capability or "BASIC")
    log_event(log, "AMBULANCE_CANDIDATES", incident_id=str(inc.id),
              candidates=[{"id": c.ambulance_id, "score": round(c.score, 3), "eta_s": round(c.eta_s)} for c in ranked])
    return ranked, routes


def _dispatch(db: Session, inc: EmergencyIncident, chosen: ScoredCandidate, rr: RouteResult, ranked: list[ScoredCandidate],
              method: str, decision_ms: float, note: str | None = None) -> Dispatch:
    amb = db.get(Ambulance, chosen.ambulance_id, with_for_update=True)
    if amb is None or amb.status != "AVAILABLE":
        raise NoAmbulanceAvailable(f"{chosen.ambulance_id} is no longer available")
    order = [chosen] + [c for c in ranked if c.ambulance_id != chosen.ambulance_id]
    explanation = explain_dispatch(order)
    if not chosen.suitable:
        explanation += "\nWARNING: no unit with the required capability was available."
    if note:
        explanation += "\n" + note
    if rr.shortest_distance_m is None:
        rr.shortest_distance_m = require_router().shortest_distance((amb.latitude, amb.longitude),
                                                                    (inc.latitude, inc.longitude))
    dispatch = Dispatch(id=uuid.uuid4(), incident_id=inc.id, ambulance_id=amb.id, method=method,
                        dispatch_score=chosen.score, eta_to_patient_s=chosen.eta_s, distance_to_patient_m=chosen.distance_m,
                        candidates=[c.as_dict() for c in ranked], explanation=explanation, decision_ms=decision_ms)
    db.add(dispatch)
    db.flush()
    origin = (amb.latitude, amb.longitude)
    dest = (inc.latitude, inc.longitude)
    route = save_route(db, rr, leg="TO_PATIENT", origin=origin, dest=dest, ambulance_id=amb.id, incident_id=inc.id,
                       dispatch_id=dispatch.id)
    now = utcnow()
    inc.assigned_ambulance = amb.id
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
    activate(db, route, rr, amb.id, inc.id, "TO_PATIENT", dest)
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
        waiting = refresh_waiting_priorities(db)
        if not waiting:
            return dispatched
        available = db.scalar(text("SELECT count(*) FROM ambulances WHERE status='AVAILABLE' "
                                   "AND driver_status='ON_DUTY' AND fuel_level >= 10")) or 0
        if available == 0:
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
            except NoAmbulanceAvailable as exc:
                emit(db, "DISPATCH_PENDING", {"incident_id": str(inc.id), "reference": inc.reference, "reason": str(exc)},
                     incident_id=inc.id, persist=False)
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
            try:
                with db.begin_nested():
                    _dispatch(db, by_id[iid], chosen, routes[amb_id], ranked, method, decision_ms, note)
                dispatched.append(iid)
            except NoAmbulanceAvailable as exc:
                log_event(log, "DISPATCH_SKIPPED", incident_id=iid, reason=str(exc))
    return dispatched


def preview(db: Session, inc: EmergencyIncident) -> dict:
    ranked, routes = evaluate_candidates(db, inc)
    best = ranked[0]
    return {"recommended": best.ambulance_id, "explanation": explain_dispatch(ranked),
            "candidates": [dict(c.as_dict(), route=routes[c.ambulance_id].summary()) for c in ranked]}


def eta_minutes(seconds: float) -> float | None:
    return None if seconds is None or math.isinf(seconds) else round(seconds / 60, 2)
