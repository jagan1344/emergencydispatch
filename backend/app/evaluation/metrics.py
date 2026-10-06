"""Metric definitions (all times in SIMULATED seconds). Every value is measured in the simulation; nothing is
estimated after the fact. NULL (None) means "not measurable in this scenario" (e.g. no CRITICAL patient, no
re-route), never zero.

Per incident i (created at c_i, first assigned at d_i, unit arrives at a_i, patient picked up at p_i):
  response_time_s          a_i - c_i                       emergency creation -> ambulance arrival at the scene
  dispatch_delay_s         d_i - c_i                       queueing + review time before an ambulance is assigned
  patient_wait_s           p_i - c_i                       creation -> patient pickup = response time + on-scene
                                                           time (on-scene time is scenario-defined, identical
                                                           for all strategies)
  planned_response_s       dispatch_delay + initial ETA    what the system expected at dispatch time
  initial_eta_s            ETA estimated by the strategy's own router when the first unit was assigned
  final_eta_s              arrival time expected by the last plan (after re-routes), measured from dispatch
  actual_travel_s          a_i - (time the arriving unit was assigned)
  eta_error_s              actual_travel_s - final_eta_s   (positive = later than planned)
  reroutes                 accepted proactive re-routes (both legs)
  reroute_saved_s          sum of (old ETA - new ETA) over accepted re-routes with a finite old ETA
  reroute_improvement_pct  mean of 100 (old - new) / old over those re-routes
  reactive_detours         re-plans forced by physically reaching a closed road (no proactive re-routing)
  hospital_wait_predicted_s  ED wait predicted by the hospital model when the hospital was chosen (only
                           strategies that use hospital prediction)
  hospital_wait_simulated_s  ED wait in the simulation at arrival: queue model of the hospital module
                           (slot * rho / (1 - rho), rho = simulated load / capacity) - "simulated", not actual
  hospital_capability_gap  1 if the chosen hospital lacks a capability required by the SIMULATED TRUE severity
  unit_capability_match    capability match of the first dispatched unit against the need implied by the SIMULATED
                           TRUE severity (1 perfect, 0.5 acceptable, 0 unsuitable - DispatchScore table)
  critical_delay_s         max(0, response - CRITICAL_TARGET) for patients whose simulated true severity is CRITICAL
Per scenario (window W = call window of the scenario, identical for all strategies):
  ambulance_utilization    sum over units of busy time inside W / (units * W); busy = committed or on a mission
  no_unit_available_pct    share of W with zero AVAILABLE units
  reserve_availability     time-average number of AVAILABLE units / fleet size
  review_rate              share of calls held for human review (HUMAN_REVIEW)
  automatic_decision_rate  share of dispatch decisions taken without a dispatcher (1 - interventions / decisions)
  potentially_inappropriate_auto  calls whose ML confidence was LOW (< DISPATCH_CONFIDENCE_LOW) but that were
                           dispatched without human review. Operational metric - no clinical ground truth.
  under_triage_rate        share of calls whose severity used by the system is lower than the simulated label
                           (generator label, NOT a clinical truth; NULL when the strategy uses no triage)
"""
from __future__ import annotations

import math
import statistics

from app.dispatch.scoring import capability_match
from app.dispatch.severity import required_capability

SEV_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# metric -> (label, unit, direction): direction -1 = lower is better, +1 = higher is better, 0 = no preference
METRICS = {
    "response_time_s": ("Response time", "s", -1),
    "patient_wait_s": ("Patient waiting (to pickup)", "s", -1),
    "dispatch_delay_s": ("Dispatch delay (queue + review)", "s", -1),
    "initial_eta_s": ("Initial route ETA", "s", 0),
    "actual_travel_s": ("Actual travel to scene", "s", -1),
    "eta_abs_error_s": ("|ETA error| (actual - planned)", "s", -1),
    "reroutes": ("Proactive re-routes", "count", 0),
    "reroute_saved_s": ("Re-route ETA savings", "s", 1),
    "reroute_improvement_pct": ("Re-route improvement", "%", 1),
    "reactive_detours": ("Reactive detours at closures", "count", -1),
    "ambulance_utilization": ("Ambulance utilization", "ratio", 0),
    "no_unit_available_pct": ("Time with no unit available", "%", -1),
    "reserve_availability": ("Reserve availability", "ratio", 1),
    "hospital_wait_simulated_s": ("Hospital wait (simulated)", "s", -1),
    "hospital_wait_predicted_s": ("Hospital wait (predicted)", "s", 0),
    "hospital_capability_gap_pct": ("Hospital capability gap", "%", -1),
    "unsuitable_unit_pct": ("Unsuitable first unit (vs simulated need)", "%", -1),
    "unit_full_match_pct": ("Perfect unit capability match", "%", 1),
    "time_to_treatment_s": ("Creation -> ED treatment (simulated)", "s", -1),
    "critical_response_s": ("Critical response time", "s", -1),
    "critical_delay_s": ("Critical delay beyond target", "s", -1),
    "critical_delayed_pct": ("Critical cases over target", "%", -1),
    "critical_worst_delay_s": ("Worst critical delay", "s", -1),
    "review_rate": ("Review-trigger rate", "%", 0),
    "automatic_decision_rate": ("Automatic decision rate", "%", 0),
    "potentially_inappropriate_auto": ("Potentially inappropriate auto-dispatch", "count", -1),
    "under_triage_pct": ("Under-triage vs simulated label", "%", -1),
    "resource_conflicts": ("Resource conflicts detected", "count", 0),
    "reallocations": ("Automatic reallocations", "count", 0),
    "conflicts_escalated": ("Conflicts escalated", "count", 0),
    "donor_delay_s": ("Delay imposed on donor missions (est.)", "s", -1),
    "critical_delay_avoided_s": ("Requester delay avoided (est.)", "s", 1),
    "manual_interventions": ("Manual interventions", "count", -1),
    "unserved_calls": ("Calls not reached", "count", -1),
    "decision_ms": ("Decision compute time", "ms", 0),
}

SUMMARY_METRICS = ["response_time_s", "patient_wait_s", "initial_eta_s", "actual_travel_s", "reroute_saved_s",
                   "ambulance_utilization", "hospital_wait_simulated_s", "critical_delay_s", "critical_delayed_pct",
                   "review_rate", "resource_conflicts", "manual_interventions"]


def _mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return statistics.fmean(xs) if xs else None


def _pct(flags: list[bool]) -> float | None:
    return 100 * sum(flags) / len(flags) if flags else None


def incident_records(sim, strategy_name: str) -> list[dict]:
    target = sim.p.critical_target_s
    low = sim.st.dispatch_confidence_low
    out = []
    for i in sim.incs:
        c = i.spec["t"]
        resp = None if i.arrival_t is None else i.arrival_t - c
        travel = None if i.arrival_t is None or i.dispatch_t is None else i.arrival_t - i.dispatch_t
        tr = i.triage
        conf = tr.prediction.confidence if tr.prediction is not None else None
        rr = [r for r in i.reroutes if r["saved_s"] is not None]
        true_crit = i.spec["true_severity"] == "CRITICAL"
        out.append({
            "strategy": strategy_name, "scenario": sim.sc["number"], "incident": i.id,
            "emergency_type": i.spec["case"]["emergency_type"], "true_severity": i.spec["true_severity"],
            "ml_severity": tr.ml_level, "ml_confidence": None if conf is None else round(conf, 4),
            "confidence_level": tr.assessment.confidence_level, "system_severity": i.severity,
            "decision_mode": i.decision_mode, "reviewed": i.reviewed,
            "created_s": c, "dispatch_delay_s": None if i.first_dispatch_t is None else round(i.first_dispatch_t - c, 1),
            "initial_eta_s": None if i.initial_eta_s is None else round(i.initial_eta_s, 1),
            "planned_response_s": None if i.initial_eta_s is None else round(i.first_dispatch_t - c + i.initial_eta_s, 1),
            "final_eta_s": None if i.final_eta_s is None else round(i.final_eta_s, 1),
            "actual_travel_s": None if travel is None else round(travel, 1),
            "eta_error_s": None if travel is None or i.final_eta_s is None else round(travel - i.final_eta_s, 1),
            "response_time_s": None if resp is None else round(resp, 1),
            "patient_wait_s": None if i.pickup_t is None else round(i.pickup_t - c, 1),
            "dispatches": i.dispatches, "reallocated_away": i.reallocated_away,
            "reroutes": len(i.reroutes), "reroute_saved_s": round(sum(r["saved_s"] for r in rr), 1) if rr else 0.0,
            "reroute_improvement_pct": _mean([r["improvement_pct"] for r in rr]),
            "reactive_detours": i.detours, "hospital_id": i.hospital_id,
            "report_quality": i.spec.get("report_quality"), "first_unit_equipment": i.first_unit_equipment,
            "unit_capability_match": None if i.first_unit_equipment is None else capability_match(
                required_capability(i.spec["true_severity"], i.spec["case"]["emergency_type"]), i.first_unit_equipment),
            "hospital_capability_gap": None if i.hospital_id is None else int(bool(i.hospital_missing)),
            "hospital_missing": ",".join(i.hospital_missing),
            "hospital_wait_predicted_s": None if i.hospital_wait_pred_s is None else round(i.hospital_wait_pred_s, 1),
            "hospital_wait_simulated_s": None if i.hospital_wait_sim_s is None else round(i.hospital_wait_sim_s, 1),
            "time_to_treatment_s": None if i.hospital_arrival_t is None or i.hospital_wait_sim_s is None
            else round(i.hospital_arrival_t + i.hospital_wait_sim_s - c, 1),
            "critical_delay_s": None if not true_crit or resp is None else round(max(0.0, resp - target), 1),
            "low_confidence": conf is not None and conf < low,
            "auto_without_review": not i.reviewed and i.first_dispatch_t is not None,
            "under_triage": None if i.severity is None else SEV_RANK[i.severity] < SEV_RANK[i.spec["true_severity"]],
            "explanations": i.explanations, "completed": i.done_t is not None,
        })
    return out


def _busy_time(timeline: list, W: float) -> tuple[float, float]:
    """(busy seconds, available seconds) inside [0, W] from a status timeline [(t, status), ...]."""
    busy = avail = 0.0
    for k, (t0, status) in enumerate(timeline):
        t1 = timeline[k + 1][0] if k + 1 < len(timeline) else math.inf
        lo, hi = max(0.0, t0), min(W, t1)
        if hi <= lo:
            continue
        if status in ("COMMITTED", "TO_PATIENT", "ON_SCENE", "TO_HOSPITAL", "HANDOVER"):
            busy += hi - lo
        elif status == "AVAILABLE":
            avail += hi - lo
    return busy, avail


def _no_unit_share(units, W: float) -> float:
    """Share of [0, W] during which no unit was AVAILABLE (exact, from status change points)."""
    points = sorted({0.0, W} | {t for u in units for t, _ in u.timeline if 0 <= t <= W})
    none = 0.0
    for a, b in zip(points, points[1:]):
        mid = (a + b) / 2
        if not any(_status_at(u.timeline, mid) == "AVAILABLE" for u in units):
            none += b - a
    return none / W if W > 0 else 0.0


def _status_at(timeline, t):
    s = timeline[0][1]
    for t0, st in timeline:
        if t0 <= t:
            s = st
        else:
            break
    return s


def scenario_metrics(sim, records: list[dict]) -> dict:
    W = sim.sc["duration_s"]
    units = list(sim.units.values())
    busy = [(u.id, *_busy_time(u.timeline, W)) for u in units]
    served = [r for r in records if r["response_time_s"] is not None]
    crit = [r for r in records if r["true_severity"] == "CRITICAL"]
    crit_served = [r for r in crit if r["response_time_s"] is not None]
    target = sim.p.critical_target_s
    n = len(records)
    reviews = sim.interventions["review"]
    manual = sum(sim.interventions.values())
    decisions = sum(r["dispatches"] for r in records)
    conflicts = sim.conflicts
    reroute_recs = [rr for i in sim.incs for rr in i.reroutes if rr["saved_s"] is not None]
    hosp = [r for r in records if r["hospital_capability_gap"] is not None]
    return {
        "calls": n, "served_calls": len(served), "unserved_calls": n - len(served),
        "response_time_s": _mean([r["response_time_s"] for r in records]),
        "patient_wait_s": _mean([r["patient_wait_s"] for r in records]),
        "dispatch_delay_s": _mean([r["dispatch_delay_s"] for r in records]),
        "initial_eta_s": _mean([r["initial_eta_s"] for r in records]),
        "final_eta_s": _mean([r["final_eta_s"] for r in records]),
        "actual_travel_s": _mean([r["actual_travel_s"] for r in records]),
        "eta_abs_error_s": _mean([abs(r["eta_error_s"]) for r in records if r["eta_error_s"] is not None]),
        "reroutes": sum(r["reroutes"] for r in records),
        "reroute_saved_s": round(sum(x["saved_s"] for x in reroute_recs), 1),
        "reroute_improvement_pct": _mean([x["improvement_pct"] for x in reroute_recs]),
        "reactive_detours": sum(r["reactive_detours"] for r in records),
        "ambulance_utilization": sum(b for _, b, _ in busy) / (len(units) * W) if units and W else None,
        "utilization_by_unit": {uid: round(b / W, 4) for uid, b, _ in busy},
        "no_unit_available_pct": 100 * _no_unit_share(units, W),
        "reserve_availability": sum(a for _, _, a in busy) / (len(units) * W) if units and W else None,
        "hospital_wait_simulated_s": _mean([r["hospital_wait_simulated_s"] for r in records]),
        "hospital_wait_predicted_s": _mean([r["hospital_wait_predicted_s"] for r in records]),
        "hospital_capability_gap_pct": 100 * _mean([r["hospital_capability_gap"] for r in hosp]) if hosp else None,
        "time_to_treatment_s": _mean([r["time_to_treatment_s"] for r in records]),
        "unsuitable_unit_pct": _pct([r["unit_capability_match"] == 0 for r in records if r["unit_capability_match"] is not None]),
        "unit_full_match_pct": _pct([r["unit_capability_match"] == 1 for r in records if r["unit_capability_match"] is not None]),
        "critical_calls": len(crit),
        "critical_response_s": _mean([r["response_time_s"] for r in crit]),
        "critical_delay_s": _mean([r["critical_delay_s"] for r in crit_served]) if crit_served else None,
        "critical_delayed_pct": 100 * sum(1 for r in crit_served if r["response_time_s"] > target) / len(crit_served)
        if crit_served else None,
        "critical_worst_delay_s": max((r["critical_delay_s"] for r in crit_served), default=None),
        "review_rate": 100 * sum(1 for i in sim.incs if i.decision_mode in ("HUMAN_REVIEW", "HUMAN_REVIEW_TIMEOUT")
                                 or i.reviewed) / n if n else None,
        "automatic_decision_rate": 100 * (1 - manual / decisions) if decisions else None,
        "potentially_inappropriate_auto": sum(1 for r in records if r["low_confidence"] and r["auto_without_review"]),
        "under_triage_pct": 100 * _mean([int(r["under_triage"]) for r in records if r["under_triage"] is not None])
        if any(r["under_triage"] is not None for r in records) else None,
        "resource_conflicts": len(conflicts),
        "reallocations": sum(1 for c in conflicts if c["decision"] in ("REALLOCATED", "APPROVED")),
        "conflicts_escalated": sum(1 for c in conflicts if c["decision"] in ("APPROVED", "REJECTED")),
        "conflicts_approved": sum(1 for c in conflicts if c["decision"] == "APPROVED"),
        "conflicts_rejected": sum(1 for c in conflicts if c["decision"] == "REJECTED"),
        "donor_delay_s": _mean([c["donor_delay_s"] for c in conflicts if c["decision"] in ("REALLOCATED", "APPROVED")]),
        "critical_delay_avoided_s": _mean([c["requester_delay_avoided_s"] for c in conflicts
                                           if c["decision"] in ("REALLOCATED", "APPROVED")]),
        "manual_interventions": manual, "interventions": dict(sim.interventions),
        "dispatch_decisions": decisions,
        "decision_ms": _mean(sim.decision_ms), "explain_ms": _mean(sim.explain_ms),
        "explanations": sum(r["explanations"] for r in records),
        "traffic_predictions": sim.counters["traffic_predictions"], "route_checks": sim.counters["route_checks"],
        "reroute_evaluations": sim.counters["reroute_evaluations"], "sim_end_s": round(sim.end_t, 1),
    }
