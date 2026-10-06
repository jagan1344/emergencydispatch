"""Dynamic multi-emergency resource reallocation (priority-aware, never unsafe).

When a waiting incident's best FREE suitable ambulance is missing or slower than
RESOURCE_REALLOCATION_THRESHOLD, ambulances already driving to a LOWER-priority incident are considered:

  candidate donor unit u (on its way to incident D, leg TO_PATIENT, not yet at the scene):
    * severity(D) < severity(new)             strictly less severe   (never take from an equal or more critical case)
    * priority(new) - priority(D) >= REALLOCATION_PRIORITY_MARGIN
    * u's equipment is suitable for the new incident
    * ETA(u -> new, from its live position) <= best free ETA - REALLOCATION_MIN_GAIN
  impact on D: replacement ETA (best free unit for D) - D's current remaining ETA
    * replacement exists and impact <= REALLOCATION_MAX_DONOR_DELAY  -> REALLOCATED automatically
                                                                        (u -> new incident, replacement -> D)
    * otherwise                                                      -> ESCALATED to the dispatcher (no change)
Every decision is stored in resource_conflicts with ETAs, impact and the reason, and emitted as an event.
"""
from __future__ import annotations

import logging
import math
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch.scoring import CandidateInput, ScoredCandidate, capability_match, score_candidates
from app.models import Ambulance, Dispatch, EmergencyIncident, ResourceConflict, Route
from app.routing.engine import NoRouteError, Segment
from app.services.events import emit
from app.services.state import STATE, require_router
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.reallocation")
SEV_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _m(s: float | None) -> str:
    return "∞" if s is None or math.isinf(s) else f"{s / 60:.1f} min"


def _route_from_live_position(ar, dest):
    """Real road route from the unit's live position (finishing its current edge first)."""
    from app.services.routes_service import position_on_route
    router = require_router()
    g = STATE.graph
    k, frac, point = position_on_route(ar, ar.progress_m)
    seg = ar.segments[k]
    prefix, origin_node = [], None
    if seg.to_node is not None and seg.to_node in g.idx_of:
        left = seg.length_m * (1 - frac)
        if left > 0.5:
            prefix = router.recost([Segment(seg.road_id, seg.from_node, seg.to_node, left, seg.base_speed_kph,
                                            seg.adj_speed_kph or seg.base_speed_kph, [point, seg.coords[-1]])])
            if prefix[0].adj_speed_kph <= 0:
                p = prefix[0]
                prefix = [Segment(p.road_id, p.from_node, p.to_node, p.length_m, p.base_speed_kph, p.base_speed_kph, p.coords)]
        origin_node = g.idx_of[seg.to_node]
    return point, router.route(point, dest, origin_node=origin_node, prefix=prefix)


# ---- pure policy (shared by consider_reallocation and the evaluation simulator)
def needs_reallocation(best_free_eta: float, st) -> bool:
    """Only considered when the best free suitable unit is missing or slower than the threshold."""
    return best_free_eta > st.resource_reallocation_threshold_s


def donor_eligible(requester_severity: str, requester_priority: float, donor_severity: str, donor_priority: float,
                   required_capability: str, equipment_level: str, st) -> bool:
    """A committed unit may be considered only if the donor incident is STRICTLY less severe, the requester's
    priority exceeds it by the margin, and the unit is suitable for the requester."""
    if SEV_RANK.get(donor_severity, 0) >= SEV_RANK.get(requester_severity, 0):
        return False                       # never take a unit from an equally or more critical emergency
    if (requester_priority or 0) - (donor_priority or 0) < st.reallocation_priority_margin:
        return False
    return capability_match(required_capability, equipment_level) > 0


def gain_sufficient(best_free_eta: float, diverted_eta: float, st) -> bool:
    return best_free_eta - diverted_eta >= st.reallocation_min_gain_s


def reallocation_outcome(replacement_exists: bool, donor_impact_s: float, st) -> str:
    """REALLOCATED (automatic) only with a replacement and a bounded donor delay, otherwise ESCALATED."""
    return "REALLOCATED" if replacement_exists and donor_impact_s <= st.reallocation_max_donor_delay_s else "ESCALATED"


def _open_conflict(db: Session, to_id, amb_id: str) -> bool:
    return db.scalar(select(ResourceConflict).where(
        ResourceConflict.to_incident_id == to_id, ResourceConflict.ambulance_id == amb_id,
        ResourceConflict.decision.in_(("ESCALATED", "REJECTED")))) is not None


def consider_reallocation(db: Session, inc: EmergencyIncident, ranked: list[ScoredCandidate] | None) -> bool:
    """Returns True when the incident was dispatched through a reallocation."""
    st = get_settings()
    if not st.reallocation_enabled or STATE.router is None or inc.status != "WAITING":
        return False
    from app.services.dispatch_service import NoAmbulanceAvailable, evaluate_candidates
    from app.services.routes_service import ACTIVE, decision_eta
    best_free = next((c for c in (ranked or []) if c.suitable), None)
    alt_eta = best_free.eta_s if best_free else math.inf
    if not needs_reallocation(alt_eta, st):
        return False
    req = inc.required_capability or "BASIC"
    options = []
    for ar in ACTIVE.all():
        if ar.leg != "TO_PATIENT" or ar.incident_id is None or ar.incident_id == inc.id:
            continue
        donor = db.get(EmergencyIncident, ar.incident_id)
        amb = db.get(Ambulance, ar.ambulance_id)
        if donor is None or amb is None or donor.status not in ("DISPATCHED", "EN_ROUTE"):
            continue
        if not donor_eligible(inc.severity, inc.priority, donor.severity, donor.priority, req, amb.equipment_level, st):
            continue
        try:
            point, rr = _route_from_live_position(ar, (inc.latitude, inc.longitude))
        except NoRouteError:
            continue
        if not gain_sufficient(alt_eta, rr.eta_s, st):
            continue
        options.append((rr.eta_s, -(donor.priority or 0), ar, donor, amb, point, rr))
    if not options:
        return False
    to_eta, _, ar, donor, amb, point, rr = min(options, key=lambda o: (o[0], o[1]))
    donor_before = decision_eta(ar)
    log_event(log, "RESOURCE_CONFLICT_DETECTED", ambulance_id=amb.id, from_incident=str(donor.id),
              to_incident=str(inc.id), to_eta_s=round(to_eta, 1), alternative_eta_s=None if math.isinf(alt_eta) else round(alt_eta, 1))
    # impact on the donor incident: its best replacement from the free fleet
    try:
        d_ranked, d_routes = evaluate_candidates(db, donor)
        repl = next((c for c in d_ranked if c.suitable), d_ranked[0])
    except NoAmbulanceAvailable:
        d_ranked, d_routes, repl = [], {}, None
    donor_after = repl.eta_s if repl else math.inf
    impact = donor_after - donor_before
    details = {"requester": {"reference": inc.reference, "severity": inc.severity, "priority": inc.priority},
               "donor": {"reference": donor.reference, "severity": donor.severity, "priority": donor.priority},
               "replacement_ambulance": repl.ambulance_id if repl else None,
               "policy": {"threshold_s": st.resource_reallocation_threshold_s, "min_gain_s": st.reallocation_min_gain_s,
                          "max_donor_delay_s": st.reallocation_max_donor_delay_s,
                          "priority_margin": st.reallocation_priority_margin}}
    base_reason = (f"{inc.reference} ({inc.severity}, priority {inc.priority:.0f}) outranks {donor.reference} "
                   f"({donor.severity}, priority {donor.priority:.0f}); best free compatible unit "
                   + (f"needs {_m(alt_eta)}" if math.isfinite(alt_eta) else "does not exist")
                   + f" (threshold {_m(st.resource_reallocation_threshold_s)}), {amb.id} can arrive in {_m(to_eta)}")
    auto = reallocation_outcome(repl is not None, impact, st) == "REALLOCATED"
    if not auto:
        if _open_conflict(db, inc.id, amb.id):
            return False
        why = (f"no replacement ambulance for {donor.reference}" if repl is None else
               f"{donor.reference} would be delayed by {_m(impact)} (> {_m(st.reallocation_max_donor_delay_s)} allowed)")
        c = ResourceConflict(ambulance_id=amb.id, from_incident_id=donor.id, to_incident_id=inc.id, kind="REALLOCATION",
                             decision="ESCALATED", reason=base_reason + f". Not automatic: {why} - dispatcher decision required.",
                             from_eta_before_s=donor_before, from_eta_after_s=None if math.isinf(donor_after) else donor_after,
                             to_eta_s=to_eta, alternative_eta_s=None if math.isinf(alt_eta) else alt_eta, details=details)
        db.add(c)
        db.flush()
        for iid in (inc.id, donor.id):
            emit(db, "RESOURCE_ESCALATED", {"conflict_id": str(c.id), "ambulance_id": amb.id, "reason": c.reason,
                                            "from_incident": donor.reference, "to_incident": inc.reference},
                 incident_id=iid, ambulance_id=amb.id)
        from app.services.dispatch_service import _note
        _note(db, inc, f"reallocation of {amb.id} from {donor.reference} needs dispatcher approval: {why}"
                       + (f"; meanwhile the best free unit is dispatched" if best_free else ""))
        log_event(log, "RESOURCE_ESCALATED", ambulance_id=amb.id, from_incident=str(donor.id), to_incident=str(inc.id), reason=why)
        return False
    execute_reallocation(db, inc, donor, amb, ar, point, rr, alt_eta, donor_before, repl, d_ranked, d_routes,
                         base_reason + f". Impact: {donor.reference} ETA {'+' if impact >= 0 else ''}{impact / 60:.1f} min "
                         f"with replacement {repl.ambulance_id}.", details)
    return True


def execute_reallocation(db: Session, inc, donor, amb, ar, point, rr, alt_eta, donor_before, repl, d_ranked, d_routes,
                         reason: str, details: dict, approved_by: str | None = None, repl_ar=None,
                         repl_origin=None, existing: ResourceConflict | None = None) -> ResourceConflict:
    from app.services.dispatch_service import _dispatch, _note
    from app.services.incident_service import set_status
    t0 = time.perf_counter()
    # 1) release the donor incident
    disp = db.scalar(select(Dispatch).where(Dispatch.incident_id == donor.id, Dispatch.status == "ACTIVE"))
    if disp:
        disp.status = "REALLOCATED"
    old = db.get(Route, ar.route_id)
    if old is not None:
        old.active, old.superseded_at = False, utcnow()
    donor.assigned_ambulance, donor.dispatched_at = None, None
    set_status(db, donor, "WAITING", reason=f"{amb.id} reallocated to {inc.reference}")
    _note(db, donor, f"{amb.id} was reallocated to higher-priority {inc.reference}; replacement being dispatched")
    amb.current_incident = None
    # 2) the unit drives to the new incident from its live position (simulator: ROUTE_UPDATED, no teleport)
    sc = score_candidates([CandidateInput(amb.id, amb.equipment_level, rr.eta_s, rr.distance_m,
                                          max(0.0, rr.eta_s - rr.base_duration_s), amb.missions_today, amb.fuel_level,
                                          extra={"fuel_level": amb.fuel_level, "missions_today": amb.missions_today,
                                                 "reallocated_from": donor.reference})], inc.required_capability or "BASIC")[0]
    was_moving = amb.status == "EN_ROUTE_TO_PATIENT"
    _dispatch(db, inc, sc, rr, [sc], "REALLOCATION", (time.perf_counter() - t0) * 1000,
              note=f"REALLOCATED from {donor.reference}: {reason}", reallocated_from=ar, origin=point)
    if was_moving:   # already driving: the simulator continues (no new ROUTE_STARTED), so set EN_ROUTE now
        from app.services.mission_service import set_amb_status
        set_amb_status(db, amb, "EN_ROUTE_TO_PATIENT", inc.id)
        set_status(db, inc, "EN_ROUTE", ambulance_id=amb.id)
    # 3) replacement for the donor
    donor_after = None
    if repl is not None:
        _dispatch(db, donor, repl, d_routes[repl.ambulance_id], d_ranked, "REALLOCATION_REPLACEMENT",
                  (time.perf_counter() - t0) * 1000, note=f"replacement after {amb.id} was reallocated to {inc.reference}",
                  reallocated_from=repl_ar, origin=repl_origin)
        donor_after = repl.eta_s
    if existing is not None:          # an approved escalation is resolved in place (one record per conflict)
        c = existing
        c.decision, c.reason, c.resolved_by, c.resolved_at = "APPROVED", reason, approved_by, utcnow()
        c.from_eta_before_s, c.from_eta_after_s, c.to_eta_s = donor_before, donor_after, rr.eta_s
    else:
        c = ResourceConflict(ambulance_id=amb.id, from_incident_id=donor.id, to_incident_id=inc.id, kind="REALLOCATION",
                             decision="REALLOCATED", reason=reason, from_eta_before_s=donor_before,
                             from_eta_after_s=donor_after, to_eta_s=rr.eta_s,
                             alternative_eta_s=None if math.isinf(alt_eta) else alt_eta, details=details)
        db.add(c)
    db.flush()
    for iid in (inc.id, donor.id):
        emit(db, "RESOURCE_REALLOCATED", {"conflict_id": str(c.id), "ambulance_id": amb.id, "reason": reason,
                                          "previous_incident": donor.reference, "new_incident": inc.reference,
                                          "replacement": repl.ambulance_id if repl else None,
                                          "impact_s": None if donor_after is None else round(donor_after - donor_before, 1)},
             incident_id=iid, ambulance_id=amb.id)
    log_event(log, "RESOURCE_REALLOCATED", ambulance_id=amb.id, previous_incident=str(donor.id), new_incident=str(inc.id),
              replacement=repl.ambulance_id if repl else None, reason=reason)
    return c


def approve(db: Session, conflict: ResourceConflict, user: str) -> ResourceConflict:
    """Dispatcher approves an ESCALATED reallocation.
    * requester still WAITING  -> the unit is diverted; the donor gets the best free unit (or waits in the queue)
    * requester was meanwhile served by another unit X -> SWAP: the unit goes to the requester and X (from its live
      position) takes over the donor incident, so both incidents keep an ambulance."""
    from app.dispatch.scoring import CandidateInput, score_candidates
    from app.services.dispatch_service import NoAmbulanceAvailable, evaluate_candidates
    from app.services.incident_service import set_status
    from app.services.routes_service import ACTIVE, decision_eta
    inc = db.get(EmergencyIncident, conflict.to_incident_id)
    donor = db.get(EmergencyIncident, conflict.from_incident_id)
    amb = db.get(Ambulance, conflict.ambulance_id, with_for_update=True)
    ar = ACTIVE.get(amb.id) if amb else None
    if inc is None or donor is None or amb is None or ar is None or ar.incident_id != donor.id \
            or inc.status not in ("WAITING", "DISPATCHED", "EN_ROUTE"):
        raise ValueError("the situation changed since the conflict was raised - nothing to approve")
    point, rr = _route_from_live_position(ar, (inc.latitude, inc.longitude))
    donor_before = decision_eta(ar)
    repl, d_ranked, d_routes, repl_ar, repl_origin = None, [], {}, None, None
    swapped = inc.status in ("DISPATCHED", "EN_ROUTE") and inc.assigned_ambulance and inc.assigned_ambulance != amb.id
    if swapped:
        x = db.get(Ambulance, inc.assigned_ambulance, with_for_update=True)
        x_ar = ACTIVE.get(x.id)
        # release X from the requester (it becomes the donor's replacement)
        disp = db.scalar(select(Dispatch).where(Dispatch.incident_id == inc.id, Dispatch.status == "ACTIVE"))
        if disp:
            disp.status = "REALLOCATED"
        old = db.get(Route, x_ar.route_id) if x_ar else None
        if old is not None:
            old.active, old.superseded_at = False, utcnow()
        inc.assigned_ambulance, inc.dispatched_at = None, None
        set_status(db, inc, "WAITING", reason=f"swap approved by {user}")
        x.current_incident = None
        if x_ar is not None:
            repl_origin, x_rr = _route_from_live_position(x_ar, (donor.latitude, donor.longitude))
            repl_ar = x_ar
        else:
            x_rr = require_router().route((x.latitude, x.longitude), (donor.latitude, donor.longitude))
        repl = score_candidates([CandidateInput(x.id, x.equipment_level, x_rr.eta_s, x_rr.distance_m,
                                                max(0.0, x_rr.eta_s - x_rr.base_duration_s), x.missions_today,
                                                x.fuel_level, extra={"fuel_level": x.fuel_level,
                                                                     "missions_today": x.missions_today})],
                                donor.required_capability or "BASIC")[0]
        d_ranked, d_routes = [repl], {x.id: x_rr}
    else:
        try:
            d_ranked, d_routes = evaluate_candidates(db, donor)
            repl = next((c for c in d_ranked if c.suitable), d_ranked[0])
        except NoAmbulanceAvailable:
            pass
    return execute_reallocation(db, inc, donor, amb, ar, point, rr, conflict.alternative_eta_s or math.inf,
                                donor_before, repl, d_ranked, d_routes,
                                f"approved by dispatcher {user}" + (f" (swap with {repl.ambulance_id})" if swapped else "")
                                + f": {conflict.reason}", conflict.details, approved_by=user,
                                repl_ar=repl_ar, repl_origin=repl_origin, existing=conflict)


def record_contested_unit(db: Session, wanted: ScoredCandidate, given: ScoredCandidate, holder, requester) -> None:
    """Two waiting incidents wanted the same unit; the OR-Tools assignment protected the higher-priority one."""
    reason = (f"{wanted.ambulance_id} was the best unit for both {holder.reference} ({holder.severity}, priority "
              f"{holder.priority:.0f}) and {requester.reference} ({requester.severity}, priority {requester.priority:.0f}); "
              f"it went to {holder.reference} (higher priority) and {requester.reference} received {given.ambulance_id} "
              f"(+{(given.eta_s - wanted.eta_s) / 60:.1f} min).")
    c = ResourceConflict(ambulance_id=wanted.ambulance_id, from_incident_id=holder.id, to_incident_id=requester.id,
                         kind="CONTESTED_UNIT", decision="ASSIGN_OTHER", reason=reason, to_eta_s=given.eta_s,
                         alternative_eta_s=wanted.eta_s, details={"assigned_instead": given.ambulance_id})
    db.add(c)
    emit(db, "RESOURCE_CONFLICT_DETECTED", {"ambulance_id": wanted.ambulance_id, "reason": reason,
                                            "decision": "ASSIGN_OTHER"}, incident_id=requester.id, ambulance_id=wanted.ambulance_id)
    log_event(log, "RESOURCE_CONFLICT_DETECTED", ambulance_id=wanted.ambulance_id, holder=str(holder.id),
              requester=str(requester.id), decision="ASSIGN_OTHER")
