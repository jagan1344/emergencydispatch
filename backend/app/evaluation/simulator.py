"""Deterministic discrete-time simulation of one scenario under one decision strategy.

What is simulated (identical for every strategy):  call arrivals, the simulated patients, ambulance movement on
the real road graph at the TRUE current speed of every road (congestion factors, accident multipliers,
closures), scenario disruptions, on-scene and handover times, hospital load (initial patients, background
arrivals, discharges, surges) and the review time of a (simulated) dispatcher.

What the strategy decides (with the production kernels, see strategies.py):  triage use, queue order, which
ambulance, which route, which hospital, review gating, reallocation, re-routing and explanations.

Time advances in fixed steps of `dt_s` simulated seconds; movement inside a step is exact (an arrival is
time-stamped where it happens inside the step). No wall-clock time and no random numbers are used here, so a
(scenario, strategy) pair always produces the same result.
"""
from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.dispatch.confidence import most_severe
from app.dispatch.explain import explain_selection
from app.dispatch.optimizer import optimal_assignment
from app.dispatch.priority import priority_score
from app.dispatch.scoring import (CandidateInput, HospitalInput, capability_match, explain_dispatch, explain_hospital,
                                  hospital_requirements, score_candidates, score_hospitals)
from app.dispatch.severity import required_capability
from app.ml.traffic_model import LEVELS, hw_index
from app.routing.engine import NoRouteError, RouteResult, RoutingEngine, RoutingPolicy, Segment, predicted_duration
from app.routing.graph import CLOSURE_SPEED_MPS, RoadGraph
from app.routing.traffic import CONGESTION_FACTORS
from app.services.hospital_prediction import expected_wait_min, forecast
from app.services.reallocation import (MAX_IMPACT_EVALUATIONS, donor_eligible, gain_sufficient, needs_reallocation,
                                      rank_donor_options)
from app.services.routes_service import _similarity, reroute_decision, reroute_trigger
from app.services.traffic_service import EVENT_PRESETS
from app.utils.geo import haversine_m

from app.evaluation.strategies import StrategyConfig

EQUIPMENT_AT_LEAST = {"BASIC": ("BASIC", "ADVANCED", "ICU"), "ADVANCED": ("ADVANCED", "ICU"), "ICU": ("ICU",)}
SEV = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
BUSY = ("COMMITTED", "TO_PATIENT", "ON_SCENE", "TO_HOSPITAL", "HANDOVER")


@dataclass(frozen=True)
class SimParams:
    dt_s: float = 2.0                     # simulation step (simulated seconds)
    dispatch_interval_s: float = 12.0     # live dispatcher: event-driven + every 3 wall s (= 12 sim s at x4)
    monitor_interval_s: float = 20.0      # live route monitor: every 5 wall s (= 20 sim s at x4)
    prediction_interval_s: float = 120.0  # live traffic prediction cycle: every 30 wall s (= 120 sim s at x4)
    max_extra_s: float = 4 * 3600.0       # hard stop after the call window
    escalation_policy: str = "reject"     # simulated dispatcher's answer to an ESCALATED reallocation
    critical_target_s: float = 480.0      # response-time target used for the critical-case delay metric


# ------------------------------------------------------------------------------------------ state
@dataclass
class Leg:
    kind: str                              # TO_PATIENT / TO_HOSPITAL
    segments: list[Segment]
    dest: tuple[float, float]
    started_t: float
    eta_at_plan_s: float                   # the strategy's own ETA estimate when this route was planned
    plan_t: float
    progress_m: float = 0.0
    allow_closure: bool = False
    considered: set = field(default_factory=set)
    abandoned: list = field(default_factory=list)
    last_reroute_t: float | None = None
    cum: list = field(default_factory=list)     # cumulative start distance of every segment

    def __post_init__(self):
        self.reindex()

    def reindex(self) -> None:
        c, acc = [], 0.0
        for s in self.segments:
            c.append(acc)
            acc += s.length_m
        self.cum, self.total_m = c, acc


@dataclass
class Unit:
    id: str
    equipment: str
    fuel: float
    missions_today: int
    lat: float
    lon: float
    status: str = "AVAILABLE"
    timer_until: float | None = None       # end of COMMITTED / ON_SCENE / HANDOVER / OFFLINE
    offline_after_mission_s: float | None = None
    leg: Leg | None = None
    incident: "Inc | None" = None
    hospital_id: str | None = None
    timeline: list = field(default_factory=list)   # (t, status)


@dataclass
class Inc:
    spec: dict
    triage: object
    state: str = "PENDING"                 # PENDING -> WAITING -> ASSIGNED -> ON_SCENE -> TRANSPORT -> HANDOVER -> DONE
    severity: str | None = None            # what the system believes (None = no triage used)
    required: str = "BASIC"
    decision_mode: str = "AUTO_DISPATCH"
    release_t: float = 0.0
    reviewed: bool = False
    priority: float = 0.0
    unit: Unit | None = None
    first_dispatch_t: float | None = None
    dispatch_t: float | None = None
    initial_eta_s: float | None = None
    final_eta_s: float | None = None
    arrival_t: float | None = None
    pickup_t: float | None = None
    hospital_arrival_t: float | None = None
    done_t: float | None = None
    dispatches: int = 0
    reallocated_away: int = 0
    first_unit_equipment: str | None = None
    hospital_id: str | None = None
    hospital_missing: list = field(default_factory=list)
    hospital_wait_pred_s: float | None = None
    hospital_wait_sim_s: float | None = None
    hospital_transport_eta_s: float | None = None
    reroutes: list = field(default_factory=list)
    detours: int = 0
    explanations: int = 0

    @property
    def id(self) -> str:
        return self.spec["id"]

    @property
    def point(self) -> tuple[float, float]:
        return self.spec["lat"], self.spec["lon"]


@dataclass
class HospitalSim:
    spec: dict
    transported: list = field(default_factory=list)      # (arrival_t, stay_s, incident id)
    surge: list = field(default_factory=list)            # (t, stay_s)
    incoming: dict = field(default_factory=dict)         # unit id -> callable eta (min)
    icu: int = 0

    def __post_init__(self):
        self.icu = int(self.spec["icu_available"])

    def _stays(self):
        yield from ((a, s) for a, s in self.spec["background"])
        yield from ((a, s) for a, s, _ in self.transported)
        yield from self.surge

    def load(self, t: float) -> int:
        n = sum(1 for d in self.spec["initial_discharges_s"] if d > t)
        return n + sum(1 for a, s in self._stays() if a <= t < a + s)

    def load_excluding(self, t: float, incident_id: str) -> int:
        """Simulated load at t without the patient whose forecast is being scored."""
        own = sum(1 for a, s, i in self.transported if i == incident_id and a <= t < a + s)
        return self.load(t) - own

    def observed(self, t: float, window_s: float) -> tuple[int, int]:
        lo = t - window_s
        arrivals = sum(1 for a, _ in self._stays() if lo < a <= t)
        discharges = sum(1 for d in self.spec["initial_discharges_s"] if lo < d <= t)
        discharges += sum(1 for a, s in self._stays() if lo < a + s <= t)
        return arrivals, discharges


# ------------------------------------------------------------------------------------------ simulation
class Simulation:
    def __init__(self, ctx, scenario: dict, strategy: StrategyConfig, triages: dict, params: SimParams | None = None):
        self.ctx, self.sc, self.s, self.p = ctx, scenario, strategy, params or SimParams()
        self.st = ctx.settings
        self.g: RoadGraph = ctx.base_graph.clone()
        self.g.reset_traffic()
        policy = RoutingPolicy(traffic_aware=strategy.traffic, use_prediction=strategy.traffic_prediction,
                               horizon_min=self.st.traffic_prediction_horizon_min)
        self.router = RoutingEngine(self.g, ctx.osrm, policy)
        self.t = 0.0
        self.wall0 = datetime(2026, 1, 5, scenario["start_hour"], tzinfo=timezone.utc)
        self.base_level: dict[str, str] = {}
        self.changed_t: dict[str, float] = {}
        self.accident_roads: set[str] = set()
        self.level_log: dict[str, list] = {}         # road -> [(t, level index)] ground-truth traffic history
        self.traffic_checks: list = []               # (due t, road, predicted idx, current idx, fallback idx)
        self.hospital_checks: list = []              # (hospital, due t, predicted load, current load, predicted wait s, incident)
        self.route_failure_keys: set = set()         # distinct (unit / point, call, purpose) without a drivable route
        self.closure_waits = 0                       # legs that had to stop at a closure (no detour existed)
        self.priority_violations = 0
        self.unsafe_reallocations = 0
        for lvl, roads in scenario["initial_traffic"].items():
            for r in roads:
                self.g.set_road_state(r, lvl, 1.0, False)
                self.base_level[r] = lvl
                self.changed_t[r] = -1800.0
                self.level_log[r] = [(-1800.0, LEVELS.index(lvl))]
        self.units = {u["id"]: Unit(u["id"], u["equipment_level"], u["fuel_level"], u["missions_today"], u["lat"], u["lon"])
                      for u in scenario["fleet"]}
        for u in scenario["fleet"]:
            unit = self.units[u["id"]]
            if u["committed_until_s"]:
                unit.status, unit.timer_until = "COMMITTED", u["committed_until_s"]
            unit.timeline.append((0.0, unit.status))
        self.incs = [Inc(spec, triages[spec["id"]]) for spec in scenario["incidents"]]
        self.hosp = {h["id"]: HospitalSim(h) for h in scenario["hospitals"]}
        self.events = sorted(scenario["events"], key=lambda e: (e["t"], e["kind"]))
        self.ev_i = 0
        # counters / logs
        self.conflicts: list[dict] = []
        self.escalated_pairs: set[tuple[str, str]] = set()
        self.interventions: dict[str, int] = {"review": 0, "reallocation_decision": 0, "manual_dispatch": 0,
                                              "manual_reroute": 0, "hospital_override": 0}
        self.counters: dict[str, int] = {"traffic_predictions": 0, "route_checks": 0, "reroute_evaluations": 0,
                                         "predicted_changes": 0}
        self.decision_ms: list[float] = []
        self.explain_ms: list[float] = []
        self.trace: list[dict] = []
        self._dispatch_needed = False

    # ---------------------------------------------------------------- helpers
    def log(self, event: str, /, **data) -> None:
        self.trace.append({"t": round(self.t, 1), "event": event, **data})

    def _set_status(self, u: Unit, status: str) -> None:
        if u.status != status:
            u.status = status
            u.timeline.append((self.t, status))

    def _eta(self, rr: RouteResult) -> float:
        """The strategy's own ETA estimate: free-flow for static routing, (predicted) traffic ETA otherwise."""
        return rr.base_duration_s if not self.s.traffic else rr.eta_s

    def _route(self, origin, dest, origin_node=None, prefix=None) -> RouteResult:
        return self.router.route(origin, dest, origin_node=origin_node, prefix=prefix, compute_shortest=False)

    def _position(self, leg: Leg) -> tuple[int, float, tuple[float, float]]:
        if not leg.segments:
            return 0, 0.0, leg.dest
        k = max(0, min(len(leg.segments) - 1, bisect.bisect_right(leg.cum, leg.progress_m) - 1))
        s = leg.segments[k]
        frac = 0.0 if s.length_m <= 0 else min(1.0, max(0.0, (leg.progress_m - leg.cum[k]) / s.length_m))
        if len(s.coords) >= 2:
            (a, b), (c, d) = s.coords[0], s.coords[-1]
            pt = (a + (c - a) * frac, b + (d - b) * frac)
        else:
            pt = s.coords[0] if s.coords else leg.dest
        return k, frac, pt

    def _route_from_live(self, u: Unit, dest) -> tuple[tuple[float, float], RouteResult]:
        """Same construction as the live system (routes_service._reroute / reallocation._route_from_live_position):
        finish the current edge, then the best route from its end node."""
        leg = u.leg
        if leg is None or not leg.segments:
            return (u.lat, u.lon), self._route((u.lat, u.lon), dest)
        k, frac, point = self._position(leg)
        seg = leg.segments[k]
        prefix, origin_node = [], None
        if seg.to_node is not None and seg.to_node in self.g.idx_of:
            left = seg.length_m * (1 - frac)
            if left > 0.5:
                prefix = self.router.recost([Segment(seg.road_id, seg.from_node, seg.to_node, left, seg.base_speed_kph,
                                                     seg.adj_speed_kph if seg.adj_speed_kph > 0 else seg.base_speed_kph,
                                                     [point, seg.coords[-1]])])
                if prefix[0].adj_speed_kph <= 0:
                    q = prefix[0]
                    prefix = [Segment(q.road_id, q.from_node, q.to_node, q.length_m, q.base_speed_kph, q.base_speed_kph, q.coords)]
            origin_node = self.g.idx_of[seg.to_node]
        return point, self._route(point, dest, origin_node=origin_node, prefix=prefix)

    def _remaining(self, leg: Leg) -> tuple[list[Segment], float]:
        return self.router.remaining(leg.segments, leg.progress_m)

    def _remaining_current(self, leg: Leg) -> float:
        return self.router.remaining_eta(leg.segments, leg.progress_m)

    def _remaining_planned(self, leg: Leg) -> float:
        return self.router.remaining_eta(leg.segments, leg.progress_m, use_planned=True)

    def _remaining_predicted(self, leg: Leg) -> float | None:
        if not (self.s.traffic and self.s.traffic_prediction):
            return None
        rem, frac = self._remaining(leg)
        if not rem:
            return 0.0
        return predicted_duration(self.router.recost(rem), self.st.traffic_prediction_horizon_min * 60, first_fraction=frac)

    def _decision_eta(self, leg: Leg) -> float:
        if not self.s.traffic:          # a static system only knows free-flow times
            rem, frac = self._remaining(leg)
            return (rem[0].base_time_s * frac + sum(x.base_time_s for x in rem[1:])) if rem else 0.0
        p = self._remaining_predicted(leg)
        return p if p is not None and math.isfinite(p) else self._remaining_current(leg)

    # ---------------------------------------------------------------- traffic
    def _apply_traffic(self, ev: dict) -> None:
        road = ev["road_id"]
        kind = ev["kind"]
        if kind == "CONGESTION":
            level, blocked, mult = ev["level"], False, 1.0
        elif kind == "CLEAR":
            level, blocked, mult = self.base_level.get(road, "FREE"), False, 1.0
        else:
            level, blocked, mult = EVENT_PRESETS[kind]
        self.g.set_road_state(road, level, mult, blocked)
        self.changed_t[road] = self.t
        self.level_log.setdefault(road, [(-1e9, 0)]).append((self.t, LEVELS.index(level)))
        if kind == "ACCIDENT":
            self.accident_roads.add(road)
        elif kind == "CLEAR":
            self.accident_roads.discard(road)
        self.log("TRAFFIC_CHANGED", road_id=road, kind=kind, level=level)
        if self.s.traffic and self.s.traffic_prediction:
            self._predict({road})
        if self.s.dynamic_rerouting:
            self._monitor(affected={road}, accident={road} & self.accident_roads)

    def _predict(self, roads: set[str] | None = None) -> None:
        """Same model and road selection as services.traffic_prediction (congested / recently changed / on routes)."""
        model = self.ctx.traffic_model
        if model is None:
            return
        H = self.st.traffic_prediction_horizon_min
        if roads is None:
            roads = {r for r, i in self.g.road_idx.items() if self.g.road_level[i] != "FREE" or self.g.road_blocked[i]}
            roads |= {r for r, t in self.changed_t.items() if self.t - t <= H * 60}
            roads |= {s.road_id for u in self.units.values() if u.leg for s in u.leg.segments if s.road_id}
        rows, ids = [], []
        wall = self.wall0 + timedelta(seconds=self.t)
        for r in sorted(roads):
            i = self.g.road_idx.get(r)
            if i is None:
                continue
            level = self.g.road_level[i]
            rows.append({"road_id": r, "cur": LEVELS.index(level), "blocked": bool(self.g.road_blocked[i]),
                         "accident": r in self.accident_roads, "age_min": max(0.0, (self.t - self.changed_t.get(r, -1800.0)) / 60),
                         "hw": hw_index(self.g.road_highway[i] or ""), "limit": float(self.g.road_limit_kph[i]),
                         "wall": wall, "rate": 0.0})
            ids.append(i)
        if not rows:
            return
        changed = 0
        if model.method == "FALLBACK":
            fb = [p[0] for p in model.predict(rows)]
        else:
            from app.ml.traffic_model import TrafficModel
            fb = [p[0] for p in TrafficModel(model.horizon_min, "eval-fallback", "FALLBACK", model.stats).predict(rows)]
        for row, i, (lvl_i, _conf, _method), fb_i in zip(rows, ids, model.predict(rows), fb):
            level = LEVELS[lvl_i]
            mult = float(self.g.road_multiplier[i]) if row["accident"] and level in ("SEVERE", "HEAVY") else 1.0
            self.g.set_road_prediction(row["road_id"], level, mult)
            changed += level != self.g.road_level[i]
            self.traffic_checks.append((self.t + H * 60, row["road_id"], lvl_i, row["cur"], fb_i))
        self.counters["traffic_predictions"] += len(rows)
        self.counters["predicted_changes"] += changed

    # ---------------------------------------------------------------- re-routing (telemetry-driven)
    def _monitor(self, affected: set[str] | None = None, accident: set[str] | None = None) -> None:
        for u in self.units.values():
            leg = u.leg
            if leg is None or u.status not in ("TO_PATIENT", "TO_HOSPITAL"):
                continue
            rem, _ = self._remaining(leg)
            ahead_roads = {x.road_id for x in rem[1:] if x.road_id}
            if affected is not None and not (ahead_roads & affected):
                continue
            self.counters["route_checks"] += 1
            current, planned = self._remaining_current(leg), self._remaining_planned(leg)
            predicted = self._remaining_predicted(leg)
            trig = reroute_trigger(ahead_roads, self.g.road_state, planned, current, predicted, leg.considered,
                                   accident, self.st)
            if not trig.reason:
                continue
            leg.considered |= trig.mark_considered
            old_eta = math.inf if trig.blocked_ahead else self._decision_eta(leg)
            self._evaluate_reroute(u, trig.reason, old_eta, forced=bool(trig.blocked_ahead))

    def _evaluate_reroute(self, u: Unit, reason: str, old_eta: float, forced: bool) -> None:
        leg = u.leg
        try:
            point, new = self._route_from_live(u, leg.dest)
        except NoRouteError:
            self.log("REROUTE_EVALUATED", unit=u.id, decision="NO_ALTERNATIVE", reason=reason)
            return
        self.counters["reroute_evaluations"] += 1
        new_eta = self._eta(new)
        rem, _ = self._remaining(leg)
        same = [x.road_id for x in new.segments if x.road_id] == [x.road_id for x in rem if x.road_id]
        new_roads = frozenset(x.road_id for x in new.segments if x.road_id)
        sim = max((_similarity(new_roads, ab) for ab in leg.abandoned), default=0.0)
        since = None if leg.last_reroute_t is None else self.t - leg.last_reroute_t
        ok, why = (False, "best alternative is the current route") if same else \
            reroute_decision(old_eta, new_eta, since_last_s=since, similarity=sim, forced=forced, st=self.st)
        self.log("REROUTE_EVALUATED", unit=u.id, reason=reason, accept=ok, rule=why,
                 old_eta_s=None if math.isinf(old_eta) else round(old_eta, 1), new_eta_s=round(new_eta, 1))
        if not ok:
            leg.segments = self.router.recost(leg.segments)    # live behaviour: new conditions become the baseline
            leg.reindex()
            return
        leg.abandoned.append(frozenset(x.road_id for x in rem if x.road_id))
        saved = None if math.isinf(old_eta) else old_eta - new_eta
        rec = {"t": round(self.t, 1), "leg": leg.kind, "reason": reason, "forced": forced,
               "old_eta_s": None if math.isinf(old_eta) else round(old_eta, 1), "new_eta_s": round(new_eta, 1),
               "saved_s": None if saved is None else round(saved, 1),
               "improvement_pct": None if saved is None or old_eta <= 0 else round(100 * saved / old_eta, 2)}
        if u.incident is not None:
            u.incident.reroutes.append(rec)
        self._replace_leg(u, new, point, keep=leg)
        leg_new = u.leg
        leg_new.last_reroute_t = self.t
        self.log("ROUTE_UPDATED", unit=u.id, **rec)

    def _replace_leg(self, u: Unit, rr: RouteResult, point, keep: Leg | None = None, kind: str | None = None) -> None:
        old = keep or u.leg
        leg = Leg(kind or old.kind, rr.segments, old.dest if old else None, old.started_t if old else self.t,
                  self._eta(rr), self.t, allow_closure=bool(rr.through_closure))
        if old is not None:
            leg.abandoned, leg.considered, leg.last_reroute_t = old.abandoned, old.considered, old.last_reroute_t
        u.lat, u.lon = point
        u.leg = leg
        if u.incident is not None and leg.kind == "TO_PATIENT":
            u.incident.final_eta_s = (self.t - u.incident.dispatch_t) + leg.eta_at_plan_s

    # ---------------------------------------------------------------- movement
    def _move(self, dt: float) -> None:
        for u in self.units.values():
            if u.leg is None or u.status not in ("TO_PATIENT", "TO_HOSPITAL"):
                continue
            budget = dt
            leg = u.leg
            while budget > 1e-9 and u.leg is leg:
                k, frac, _ = self._position(leg)
                if leg.progress_m >= leg.total_m - 1e-6:
                    self._arrive(u, self.t + (dt - budget))
                    break
                seg = leg.segments[k]
                left_m = seg.length_m * (1 - frac)
                spd = self._speed(seg, entering=frac * seg.length_m <= 0.01, leg=leg)
                if spd is None:          # about to enter a closed road: re-plan (reactive detour)
                    self._detour(u, k)
                    if u.leg is leg:     # no drivable way round: wait at the closure (ROUTE UNAVAILABLE)
                        if not getattr(leg, "waited", False):
                            leg.waited = True
                            self.closure_waits += 1
                        break
                    leg = u.leg
                    continue
                if left_m <= 1e-6:
                    leg.progress_m += left_m + 1e-6
                    continue
                need = left_m / spd
                if need <= budget:
                    leg.progress_m += left_m + 1e-6
                    budget -= need
                else:
                    leg.progress_m += spd * budget
                    budget = 0.0
            if u.leg is leg and leg.progress_m >= leg.total_m - 1e-6 and u.status in ("TO_PATIENT", "TO_HOSPITAL"):
                self._arrive(u, self.t + dt - budget)
            elif u.leg is not None:
                _, _, pt = self._position(u.leg)
                u.lat, u.lon = pt

    def _speed(self, seg: Segment, entering: bool, leg: Leg) -> float | None:
        if seg.road_id is None or seg.road_id not in self.g.road_idx:
            return max(seg.adj_speed_kph, 1.0) / 3.6
        i = self.g.road_idx[seg.road_id]
        v = self.g.road_speed_mps(i)
        if v > 0:
            return v
        if leg.allow_closure:
            return CLOSURE_SPEED_MPS
        if entering:
            return None
        return self.g.road_speed_mps(i, adjusted=False)     # already on the road when it closed: leave at base speed

    def _detour(self, u: Unit, k: int) -> None:
        leg = u.leg
        seg = leg.segments[k]
        origin_node = self.g.idx_of.get(seg.from_node) if seg.from_node is not None else None
        point = seg.coords[0] if seg.coords else (u.lat, u.lon)
        try:
            rr = self._route(point, leg.dest, origin_node=origin_node)
        except NoRouteError:
            self.route_failure_keys.add((u.id, u.incident.id if u.incident else None, "detour"))
            return
        if u.incident is not None:
            u.incident.detours += 1
        self.log("REACTIVE_DETOUR", unit=u.id, road_id=seg.road_id)
        self._replace_leg(u, rr, point, keep=leg)

    def _arrive(self, u: Unit, at: float) -> None:
        inc = u.incident
        leg = u.leg
        u.lat, u.lon = leg.dest
        u.leg = None
        if leg.kind == "TO_PATIENT":
            inc.arrival_t = at
            inc.pickup_t = at + inc.spec["scene_time_s"]
            inc.state = "ON_SCENE"
            self._set_status(u, "ON_SCENE")
            u.timer_until = inc.pickup_t
            self.log("ARRIVED_AT_SCENE", unit=u.id, incident=inc.id)
        else:
            h = self.hosp[u.hospital_id]
            h.incoming.pop(u.id, None)
            inc.hospital_arrival_t = at
            inc.hospital_wait_sim_s = 60 * expected_wait_min(h.load(at), h.spec["emergency_capacity"],
                                                             self.st.ed_treatment_slot_min, self.st.hospital_max_wait_min)
            h.transported.append((at, inc.spec["hospital_stay_s"], inc.id))
            inc.state = "HANDOVER"
            self._set_status(u, "HANDOVER")
            u.timer_until = at + inc.spec["handover_time_s"]
            self.log("ARRIVED_AT_HOSPITAL", unit=u.id, incident=inc.id, hospital=u.hospital_id)

    # ---------------------------------------------------------------- timers & scheduled events
    def _timers(self) -> None:
        for u in self.units.values():
            if u.timer_until is None or self.t < u.timer_until:
                continue
            u.timer_until = None
            if u.status in ("COMMITTED", "OFFLINE"):
                self._set_status(u, "AVAILABLE")
                self._dispatch_needed = True
            elif u.status == "ON_SCENE":
                self._transport(u)
            elif u.status == "HANDOVER":
                inc = u.incident
                inc.done_t, inc.state = self.t, "DONE"
                u.incident, u.hospital_id = None, None
                if u.offline_after_mission_s:
                    self._set_status(u, "OFFLINE")
                    u.timer_until, u.offline_after_mission_s = self.t + u.offline_after_mission_s, None
                else:
                    self._set_status(u, "AVAILABLE")
                    self._dispatch_needed = True

    def _apply_events(self) -> None:
        for inc in self.incs:
            if inc.state == "PENDING" and inc.spec["t"] <= self.t:
                self._intake(inc)
        while self.ev_i < len(self.events) and self.events[self.ev_i]["t"] <= self.t:
            ev = self.events[self.ev_i]
            self.ev_i += 1
            kind = ev["kind"]
            if kind in ("BLOCK", "ACCIDENT", "CONGESTION", "CLEAR"):
                self._apply_traffic(ev)
            elif kind == "AMBULANCE_UNAVAILABLE":
                self._unit_unavailable(self.units[ev["unit"]], ev["duration_s"])
            elif kind == "HOSPITAL_SURGE":
                h = self.hosp[ev["hospital_id"]]
                h.surge += [(self.t, self.st.ed_mean_stay_min * 60)] * ev["patients"]
                self.log("HOSPITAL_SURGE", hospital=ev["hospital_id"], patients=ev["patients"])

    def _unit_unavailable(self, u: Unit, duration: float) -> None:
        self.log("AMBULANCE_UNAVAILABLE", unit=u.id, status=u.status)
        if u.status in ("AVAILABLE", "COMMITTED", "OFFLINE"):
            self._set_status(u, "OFFLINE")
            u.timer_until = max(u.timer_until or 0.0, self.t + duration)
        elif u.status == "TO_PATIENT":            # breakdown on the way: the call goes back to the queue
            inc = u.incident
            inc.unit, inc.state = None, "WAITING"
            u.incident, u.leg = None, None
            self._set_status(u, "OFFLINE")
            u.timer_until = self.t + duration
            self._dispatch_needed = True
        else:                                      # patient on board / at hospital: finish, then go offline
            u.offline_after_mission_s = duration

    # ---------------------------------------------------------------- intake (triage + confidence)
    def _intake(self, inc: Inc) -> None:
        tr = inc.triage
        inc.state = "WAITING"
        inc.release_t = inc.spec["t"]
        if self.s.severity:
            inc.severity, inc.required = tr.severity, tr.required_capability
        if self.s.severity and self.s.confidence:
            inc.decision_mode = tr.assessment.decision_mode
            if inc.decision_mode == "HUMAN_REVIEW":
                delay = inc.spec["review_delay_s"]
                timeout = self.st.human_review_timeout_s
                if timeout > 0 and delay >= timeout:       # nobody reviewed in time: live timeout policy
                    inc.release_t = inc.spec["t"] + timeout
                    sev = most_severe(tr.ml_level, tr.rule.level, tr.severity)
                    inc.severity, inc.required = sev, required_capability(sev, inc.spec["case"]["emergency_type"])
                    inc.decision_mode = "HUMAN_REVIEW_TIMEOUT"
                else:                                       # simulated dispatcher review (see README assumptions)
                    inc.release_t = inc.spec["t"] + delay
                    sev = inc.spec["true_severity"]
                    inc.severity, inc.required = sev, required_capability(sev, inc.spec["case"]["emergency_type"])
                    inc.reviewed = True
                    self.interventions["review"] += 1
        self.log("CALL_RECEIVED", incident=inc.id, severity=inc.severity, decision_mode=inc.decision_mode)
        self._dispatch_needed = True

    # ---------------------------------------------------------------- dispatch
    def _free_units(self) -> list[Unit]:
        return [u for u in self.units.values() if u.status == "AVAILABLE" and u.fuel >= 10]

    def _priority(self, inc: Inc, free: list[Unit]) -> float:
        if not self.s.severity:
            return 0.0
        nearest = min((haversine_m(u.lat, u.lon, *inc.point) for u in free), default=None)
        capable = EQUIPMENT_AT_LEAST[inc.required]
        total = sum(1 for u in self.units.values() if u.equipment in capable and u.status != "OFFLINE")
        avail = sum(1 for u in free if u.equipment in capable)
        score, _ = priority_score(inc.severity, inc.triage.rule.score, self.t - inc.spec["t"], nearest, avail, total)
        return score

    def _candidates(self, inc: Inc, free: list[Unit], k: int) -> list[dict]:
        """In-memory equivalent of dispatch_service.candidate_ids + evaluate_candidates: the k nearest free units
        (straight line) plus the 2 nearest units meeting the required capability, each routed to the patient."""
        by_d = sorted(free, key=lambda u: (haversine_m(u.lat, u.lon, *inc.point), u.id))
        picked = by_d[:k]
        if self.s.severity:
            picked += [u for u in by_d if u.equipment in EQUIPMENT_AT_LEAST[inc.required]][:2]
        seen, out = set(), []
        for u in picked:
            if u.id in seen:
                continue
            seen.add(u.id)
            try:
                rr = self._route((u.lat, u.lon), inc.point)
            except NoRouteError:
                self.route_failure_keys.add((u.id, inc.id, "dispatch"))
                continue
            out.append({"unit": u, "rr": rr, "eta": self._eta(rr), "distance": rr.distance_m,
                        "suitable": capability_match(inc.required, u.equipment) > 0})
        return out

    def _rank(self, inc: Inc, cands: list[dict]) -> list[dict]:
        if not cands:
            return []
        if self.s.traffic:          # production DispatchScore on traffic-aware ETAs
            ranked = score_candidates([CandidateInput(c["unit"].id, c["unit"].equipment, c["eta"], c["distance"],
                                                      max(0.0, c["eta"] - c["rr"].base_duration_s),
                                                      c["unit"].missions_today, c["unit"].fuel) for c in cands],
                                      inc.required)
            by_id = {c["unit"].id: c for c in cands}
            out = []
            for sc in ranked:
                c = dict(by_id[sc.ambulance_id], score=sc.score, scored=sc)
                out.append(c)
            return out
        # static strategies: nearest (by road distance) - suitable units first when the capability is known
        return sorted(cands, key=lambda c: (not c["suitable"], c["distance"], c["unit"].id))

    def _dispatcher(self) -> None:
        t0 = time.perf_counter()
        waiting = [i for i in self.incs if i.state == "WAITING" and i.release_t <= self.t]
        if not waiting:
            return
        free = self._free_units()
        for i in waiting:
            i.priority = self._priority(i, free)
        waiting.sort(key=lambda i: (-i.priority, i.spec["t"], i.id) if self.s.severity else (i.spec["t"], i.id))
        if not free:
            if self.s.reallocation:
                self._consider_reallocation(waiting[0], None)
            return
        batch = waiting[:5] if self.s.batch_optimisation else waiting
        k = 6 if (self.s.batch_optimisation and len(batch) > 1) else self.st.max_dispatch_candidates
        evals = {i.id: self._rank(i, self._candidates(i, free, k)) for i in batch}
        if self.s.reallocation:
            for i in batch:
                if self._consider_reallocation(i, evals[i.id]):
                    return                      # one reallocation per cycle (live behaviour)
        by_id = {i.id: i for i in batch}
        if self.s.batch_optimisation and sum(1 for v in evals.values() if v) > 1:
            assignment = optimal_assignment(
                {iid: by_id[iid].priority for iid, r in evals.items() if r},
                {iid: {c["unit"].id: (c["score"], c["suitable"]) for c in r} for iid, r in evals.items() if r})
            method = "ORTOOLS_ASSIGNMENT"
        else:
            assignment, taken = {}, set()
            for i in batch:
                pick = next((c for c in evals[i.id] if c["unit"].id not in taken), None)
                if pick is not None:
                    assignment[i.id] = pick["unit"].id
                    taken.add(pick["unit"].id)
            method = "WEIGHTED_SCORE" if self.s.traffic else "NEAREST"
        ms = (time.perf_counter() - t0) * 1000
        dispatched = []
        for iid, uid in sorted(assignment.items(), key=lambda kv: -by_id[kv[0]].priority):
            ranked = evals[iid]
            chosen = next(c for c in ranked if c["unit"].id == uid)
            if self.units[uid].status != "AVAILABLE":
                continue
            self._dispatch(by_id[iid], chosen, ranked, method)
            dispatched.append((by_id[iid], self.units[uid]))
        self.decision_ms.append(ms)
        self._count_priority_violations(dispatched)

    def _count_priority_violations(self, dispatched: list) -> None:
        """A unit given to call X while a call Y with a strictly more severe SIMULATED label, waiting since earlier and
        released for dispatch, stays unserved although that unit could have served Y (capability > 0)."""
        still = [y for y in self.incs if y.state == "WAITING" and y.release_t <= self.t]
        for x, u in dispatched:
            for y in still:
                if (SEV.index(y.spec["true_severity"]) > SEV.index(x.spec["true_severity"]) and y.spec["t"] < x.spec["t"]
                        and capability_match(required_capability(y.spec["true_severity"], y.spec["case"]["emergency_type"]),
                                             u.equipment) > 0):
                    self.priority_violations += 1
                    break

    def _dispatch(self, inc: Inc, chosen: dict, ranked: list[dict], method: str, origin=None) -> None:
        u: Unit = chosen["unit"]
        rr: RouteResult = chosen["rr"]
        scored = [c for c in (ranked or [chosen]) if "scored" in c]
        if self.s.explainability and "scored" in chosen:
            t0 = time.perf_counter()
            explain_selection([c["scored"].as_dict() for c in scored], u.id, method)
            explain_dispatch([chosen["scored"]] + [c["scored"] for c in scored if c is not chosen])
            self.explain_ms.append((time.perf_counter() - t0) * 1000)
            inc.explanations += 1
        inc.unit, inc.state = u, "ASSIGNED"
        inc.dispatches += 1
        inc.dispatch_t = self.t
        if inc.first_dispatch_t is None:
            inc.first_dispatch_t = self.t
            inc.initial_eta_s = chosen["eta"]
            inc.first_unit_equipment = u.equipment
        u.incident = inc
        u.missions_today += 1
        start = origin or (u.lat, u.lon)
        u.leg = Leg("TO_PATIENT", rr.segments, inc.point, self.t, chosen["eta"], self.t,
                    allow_closure=bool(rr.through_closure))
        u.lat, u.lon = start
        inc.final_eta_s = chosen["eta"]
        self._set_status(u, "TO_PATIENT")
        self.log("DISPATCHED", incident=inc.id, unit=u.id, method=method, eta_s=round(chosen["eta"], 1),
                 candidates=len(ranked))

    # ---------------------------------------------------------------- reallocation (FULL)
    def _consider_reallocation(self, inc: Inc, ranked: list[dict] | None) -> bool:
        st = self.st
        best_free = next((c for c in (ranked or []) if c["suitable"]), None)
        alt_eta = best_free["eta"] if best_free else math.inf
        if not self.s.severity or not needs_reallocation(alt_eta, st):
            return False
        options = []
        for u in self.units.values():
            donor = u.incident
            if u.status != "TO_PATIENT" or donor is None or donor is inc:
                continue
            if (inc.id, u.id) in self.escalated_pairs:
                continue
            if not donor_eligible(inc.severity, inc.priority, donor.severity, donor.priority, inc.required, u.equipment, st):
                continue
            try:
                point, rr = self._route_from_live(u, inc.point)
            except NoRouteError:
                continue
            if not gain_sufficient(alt_eta, self._eta(rr), st):
                continue
            options.append({"to_eta_s": self._eta(rr), "donor_priority": donor.priority, "unit_id": u.id, "_u": u,
                            "_donor": donor, "_point": point, "_rr": rr})
        if not options:
            return False
        options.sort(key=lambda o: (o["to_eta_s"], -o["donor_priority"], o["unit_id"]))
        for o in options[:MAX_IMPACT_EVALUATIONS]:          # same bounded impact assessment as the live policy
            before = self._decision_eta(o["_u"].leg)
            free = [x for x in self._free_units() if x is not o["_u"]]
            d_ranked = self._rank(o["_donor"], self._candidates(o["_donor"], free, st.max_dispatch_candidates))
            repl = next((c for c in d_ranked if c["suitable"]), d_ranked[0] if d_ranked else None)
            o.update(impact_s=(repl["eta"] - before) if repl else math.inf, replacement=repl is not None,
                     _before=before, _repl=repl, _d_ranked=d_ranked)
        choice, outcome = rank_donor_options(options[:MAX_IMPACT_EVALUATIONS], st)
        u, donor, point, rr = choice["_u"], choice["_donor"], choice["_point"], choice["_rr"]
        to_eta, donor_before, repl, d_ranked = choice["to_eta_s"], choice["_before"], choice["_repl"], choice["_d_ranked"]
        impact = choice["impact_s"]
        rec = {"replacement_eta_s": round(repl["eta"], 1) if repl else None,
               "unsafe_vs_simulated_label": SEV.index(donor.spec["true_severity"]) >= SEV.index(inc.spec["true_severity"]),
               "t": round(self.t, 1), "unit": u.id, "requester": inc.id, "requester_severity": inc.severity,
               "donor": donor.id, "donor_severity": donor.severity, "decision": outcome,
               "requester_eta_s": round(to_eta, 1), "alternative_eta_s": None if math.isinf(alt_eta) else round(alt_eta, 1),
               "donor_eta_before_s": round(donor_before, 1),
               "donor_delay_s": None if math.isinf(impact) else round(impact, 1),
               "requester_delay_avoided_s": None if math.isinf(alt_eta) else round(alt_eta - to_eta, 1)}
        if outcome == "ESCALATED":
            self.escalated_pairs.add((inc.id, u.id))
            self.interventions["reallocation_decision"] += 1
            if self.p.escalation_policy != "approve":
                rec["decision"] = "REJECTED"
                self.conflicts.append(rec)
                self.log("RESOURCE_CONFLICT", **rec)
                return False
            rec["decision"] = "APPROVED"
        self.conflicts.append(rec)
        self.log("RESOURCE_CONFLICT", **rec)
        # execute: the unit continues from its live position to the requester, the donor gets the replacement
        if rec["unsafe_vs_simulated_label"]:
            self.unsafe_reallocations += 1
        donor.unit, donor.state = None, "WAITING"
        donor.reallocated_away += 1
        u.incident = None
        sc = score_candidates([CandidateInput(u.id, u.equipment, to_eta, rr.distance_m, max(0.0, to_eta - rr.base_duration_s),
                                              u.missions_today, u.fuel)], inc.required)[0]
        chosen = {"unit": u, "rr": rr, "eta": to_eta, "distance": rr.distance_m, "suitable": True, "scored": sc,
                  "score": sc.score}
        self._dispatch(inc, chosen, [chosen], "REALLOCATION", origin=point)
        if repl is not None:
            self._dispatch(donor, repl, d_ranked, "REALLOCATION_REPLACEMENT")
        return True

    # ---------------------------------------------------------------- hospital
    def _transport(self, u: Unit) -> None:
        inc = u.incident
        origin = inc.point
        nearest = sorted(self.hosp.values(), key=lambda h: (haversine_m(*origin, h.spec["lat"], h.spec["lon"]), h.spec["id"]))[:8]
        routes, inputs = {}, []
        for h in nearest:
            try:
                rr = self._route(origin, (h.spec["lat"], h.spec["lon"]))
            except NoRouteError:
                self.route_failure_keys.add((h.spec["id"], inc.id, "hospital"))
                continue
            routes[h.spec["id"]] = rr
            inputs.append(HospitalInput(h.spec["id"], h.spec["name"], self._eta(rr), max(0.0, self._eta(rr) - rr.base_duration_s),
                                        rr.distance_m, h.load(self.t), h.spec["emergency_capacity"], h.icu,
                                        h.spec["trauma"], h.spec["cardiac"], h.spec["stroke"]))
        if not inputs:
            self.log("HOSPITAL_WARNING", incident=inc.id, warning="no reachable hospital")
            return
        reqs = hospital_requirements(inc.severity, inc.spec["case"]["emergency_type"]) if self.s.severity else []
        if self.s.hospital_intelligence:
            W = self.st.hospital_rate_window_min * 60
            for hi in inputs:
                h = self.hosp[hi.hospital_id]
                arr, dis = h.observed(self.t, W)
                incoming = [f() for f in h.incoming.values()]
                f = forecast(hi.current_load, hi.emergency_capacity, hi.eta_s / 60, incoming, arr, dis,
                             self.st.hospital_rate_window_min, self.st.ed_mean_stay_min, self.st.ed_treatment_slot_min,
                             self.st.hospital_max_wait_min, hi.hospital_id)
                hi.predicted_load, hi.expected_wait_s, hi.forecast = f.predicted_load, f.expected_wait_min * 60, f.as_dict()
                self.hospital_checks.append((hi.hospital_id, self.t + hi.eta_s, f.predicted_load, hi.current_load,
                                             f.expected_wait_min * 60, inc.id))
            ranked = score_hospitals(inputs, reqs)
            best_id = ranked[0]["hospital_id"]
            inc.hospital_wait_pred_s = ranked[0]["expected_wait_s"]
            if self.s.explainability:
                t0 = time.perf_counter()
                explain_hospital(ranked, reqs)
                self.explain_ms.append((time.perf_counter() - t0) * 1000)
        else:
            def missing(hi):
                caps = {"icu": hi.icu_available > 0, "trauma": hi.trauma, "cardiac": hi.cardiac, "stroke": hi.stroke}
                return sum(1 for r in reqs if not caps[r])
            key = (lambda hi: (missing(hi), hi.eta_s, hi.hospital_id)) if self.s.traffic else \
                (lambda hi: (missing(hi), hi.distance_m, hi.hospital_id))
            best_id = min(inputs, key=key).hospital_id
        h = self.hosp[best_id]
        true_reqs = hospital_requirements(inc.spec["true_severity"], inc.spec["case"]["emergency_type"])
        caps = {"icu": h.icu > 0, "trauma": h.spec["trauma"], "cardiac": h.spec["cardiac"], "stroke": h.spec["stroke"]}
        inc.hospital_missing = [r for r in true_reqs if not caps[r]]
        if inc.severity == "CRITICAL" and h.icu > 0:
            h.icu -= 1
        inc.hospital_id = best_id
        rr = routes[best_id]
        inc.hospital_transport_eta_s = self._eta(rr)
        inc.state = "TRANSPORT"
        u.hospital_id = best_id
        u.leg = Leg("TO_HOSPITAL", rr.segments, (h.spec["lat"], h.spec["lon"]), self.t, self._eta(rr), self.t,
                    allow_closure=bool(rr.through_closure))
        h.incoming[u.id] = lambda u=u: (self._decision_eta(u.leg) / 60) if u.leg else 0.0
        self._set_status(u, "TO_HOSPITAL")
        self.log("HOSPITAL_SELECTED", incident=inc.id, hospital=best_id, missing=inc.hospital_missing)

    def traffic_prediction_quality(self) -> dict:
        """Every prediction whose horizon ended inside the run, scored against the simulated ground truth, next to
        the persistence baseline ("the level stays as it is now")."""
        def level_at(road, t):
            lvl = 0
            for t0, li in self.level_log.get(road, [(-1e9, 0)]):
                if t0 <= t:
                    lvl = li
                else:
                    break
            return lvl
        rows = [(p, c, level_at(r, due), fb) for due, r, p, c, fb in self.traffic_checks if due <= self.end_t]
        if not rows:
            return {}
        n = len(rows)
        changed = [x for x in rows if x[2] != x[1]]
        return {"traffic_pred_samples": n, "traffic_pred_accuracy": 100 * sum(p == a for p, _, a, _ in rows) / n,
                "traffic_fallback_accuracy": 100 * sum(f == a for _, _, a, f in rows) / n,
                "traffic_persistence_accuracy": 100 * sum(c == a for _, c, a, _ in rows) / n,
                "traffic_pred_mae": sum(abs(p - a) for p, _, a, _ in rows) / n,
                "traffic_fallback_mae": sum(abs(f - a) for _, _, a, f in rows) / n,
                "traffic_persistence_mae": sum(abs(c - a) for _, c, a, _ in rows) / n,
                "traffic_changed_samples": len(changed),
                "traffic_pred_accuracy_on_changes": 100 * sum(p == a for p, _, a, _ in changed) / len(changed) if changed else None}

    def hospital_prediction_quality(self) -> dict:
        """Every hospital forecast (all candidates, at the predicted arrival time) scored against the SIMULATED load at
        that time (excluding the evaluated patient), next to persistence ("load stays as it is now"). Waits use the
        hospital module's queue model on those loads - simulated ground truth, not real ED data."""
        st = self.st
        wait = lambda load, cap: 60 * expected_wait_min(load, cap, st.ed_treatment_slot_min, st.hospital_max_wait_min)
        rows = []
        for hid, due, pred, cur, pred_wait, iid in self.hospital_checks:
            if due > self.end_t:
                continue
            h = self.hosp[hid]
            actual = h.load_excluding(due, iid)
            cap = h.spec["emergency_capacity"]
            rows.append((abs(pred - actual), abs(cur - actual), abs(pred_wait - wait(actual, cap)),
                         abs(wait(cur, cap) - wait(actual, cap))))
        if not rows:
            return {}
        n = len(rows)
        return {"hospital_pred_samples": n, "hospital_load_pred_mae": sum(r[0] for r in rows) / n,
                "hospital_load_persistence_mae": sum(r[1] for r in rows) / n,
                "hospital_wait_pred_mae_s": sum(r[2] for r in rows) / n,
                "hospital_wait_persistence_mae_s": sum(r[3] for r in rows) / n}

    # ---------------------------------------------------------------- main loop
    def run(self) -> "Simulation":
        p = self.p
        end = self.sc["duration_s"] + p.max_extra_s
        next_dispatch = next_monitor = next_predict = 0.0
        last_call = max((i.spec["t"] for i in self.incs), default=0.0)
        while self.t <= end:
            self._apply_events()
            self._timers()
            if self.s.traffic and self.s.traffic_prediction and self.t >= next_predict:
                self._predict()
                next_predict += p.prediction_interval_s
            if self.s.dynamic_rerouting and self.t >= next_monitor:
                self._monitor()
                next_monitor += p.monitor_interval_s
            if self._dispatch_needed or self.t >= next_dispatch:
                self._dispatch_needed = False
                self._dispatcher()
                next_dispatch = self.t + p.dispatch_interval_s
            self._move(p.dt_s)
            if self.t > last_call and all(i.state == "DONE" for i in self.incs):
                break
            self.t += p.dt_s
        self.end_t = self.t
        return self
