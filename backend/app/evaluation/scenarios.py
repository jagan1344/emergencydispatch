"""Reproducible experiment scenarios.

A scenario is a self-contained episode in the configured city, defined ONLY by (seed, scenario number) and
the road network / hospital list it is generated on:

  * simulated patients    - drawn from the project's synthetic patient generator (app.ml.dataset.generate),
                            which also yields the generator's acuity label ("simulated true severity").
  * call-time information - what the caller/crew can report. A share of calls (UNCERTAIN_REPORT_SHARE) is
                            reported with observation noise (vital signs off, SpO2 not measured, AVPU/bleeding/
                            injury one grade off, symptoms missed). Triage only ever sees the REPORTED case.
                            Bursts of near-simultaneous calls create competing emergencies.
  * fleet                 - 2-10 ambulances on random road nodes with mixed equipment (BASIC/ADVANCED/ICU), fuel
                            and workload; some are committed to an external task until a given time.
  * hospitals             - the real hospitals of the database with a scenario load regime (low/medium/high),
                            ICU beds, background arrivals and lengths of stay.
  * traffic               - a regime FREE / LIGHT / MODERATE / SEVERE applied to the major roads.
  * dynamic events        - road blockages, accidents and congestion increases (placed on the likely route of
                            an incident so they matter), an ambulance becoming unavailable, hospital surges.

Each scenario uses its own RNG seeded with (seed, scenario_number), so scenario k is identical whether 10 or 500
scenarios are generated, and every strategy replays exactly the same definition.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

import numpy as np

from app.ml.dataset import generate as generate_patients
from app.routing.graph import RoadGraph

TRAFFIC_REGIMES = {          # probability, share of major roads congested, levels used
    "FREE": (0.25, 0.0, ["LIGHT"]),
    "LIGHT": (0.25, 0.12, ["LIGHT", "LIGHT", "MODERATE"]),
    "MODERATE": (0.30, 0.28, ["LIGHT", "MODERATE", "MODERATE", "HEAVY"]),
    "SEVERE": (0.20, 0.42, ["MODERATE", "HEAVY", "HEAVY", "SEVERE"]),
}
LOAD_REGIMES = {"LOW": (0.15, 0.45), "MEDIUM": (0.45, 0.75), "HIGH": (0.75, 0.98)}
EQUIPMENT_P = {"BASIC": 0.5, "ADVANCED": 0.35, "ICU": 0.15}
UNCERTAIN_REPORT_SHARE = 0.35
MAJOR = ("motorway", "trunk", "primary", "secondary", "tertiary")
CONSCIOUSNESS = ["ALERT", "VERBAL", "PAIN", "UNRESPONSIVE"]
GRADE = ["NONE", "MINOR", "MODERATE", "SEVERE"]


@dataclass(frozen=True)
class ScenarioOptions:
    duration_s: float = 1800.0          # window in which calls arrive (simulated seconds)
    min_incidents: int = 3
    max_incidents: int = 8
    min_fleet: int = 2
    max_fleet: int = 10
    scene_time_s: float = 120.0         # mean on-scene time (live setting SCENE_TIME_S)
    handover_time_s: float = 90.0       # mean hospital handover time (HANDOVER_TIME_S)
    ed_mean_stay_min: float = 240.0     # mean ED length of stay (ED_MEAN_STAY_MIN)
    review_delay_s: tuple[float, float] = (30.0, 150.0)   # simulated dispatcher review time range

    def as_dict(self) -> dict:
        return asdict(self)


def noisy_report(case: dict, rng) -> dict:
    """Observation noise of a telephone / first-look report (applied to UNCERTAIN calls only)."""
    r = dict(case)
    r["heart_rate"] = int(min(220, max(25, round(case["heart_rate"] + rng.normal(0, 12)))))
    r["respiratory_rate"] = int(min(60, max(4, round(case["respiratory_rate"] + rng.normal(0, 4)))))
    r["oxygen_saturation"] = None if rng.random() < 0.4 else int(min(100, max(60, round(case["oxygen_saturation"] + rng.normal(0, 3)))))
    for key, scale in (("consciousness", CONSCIOUSNESS), ("bleeding", GRADE), ("injury_severity", GRADE)):
        if rng.random() < 0.25:
            k = scale.index(case[key]) + (1 if rng.random() < 0.5 else -1)
            r[key] = scale[min(len(scale) - 1, max(0, k))]
    for key in ("breathing_difficulty", "chest_pain"):
        if rng.random() < 0.15:
            r[key] = not case[key]
    return r


def case_from_row(row: dict) -> dict:
    """Generator row -> the same case dict the live API receives (see simulation_service.random_case)."""
    return {"patient_age": int(row["age"]), "heart_rate": int(row["heart_rate"]),
            "respiratory_rate": int(row["respiratory_rate"]), "oxygen_saturation": int(row["oxygen_saturation"]),
            "consciousness": CONSCIOUSNESS[int(row["consciousness"])], "bleeding": GRADE[int(row["bleeding"])],
            "injury_severity": GRADE[int(row["injury_severity"])], "accident_type": row["accident_type"],
            "breathing_difficulty": bool(row["breathing_difficulty"]), "chest_pain": bool(row["chest_pain"]),
            "emergency_type": row["emergency_type"]}


class ScenarioSpace:
    """Precomputed sampling support for one road network (nodes inside the service area, major roads)."""

    def __init__(self, graph: RoadGraph, city_lat: float, city_lon: float, radius_m: float, hospitals: list[dict]):
        self.graph = graph
        dy = (graph.lat - city_lat) * 110_540.0
        dx = (graph.lon - city_lon) * 111_320.0 * math.cos(math.radians(city_lat))
        inside = np.nonzero(np.hypot(dx, dy) <= radius_m * 0.85)[0]
        self.nodes = inside if len(inside) else np.arange(len(graph.lat))
        major = [r for r, hw in zip(graph.road_ids, graph.road_highway) if (hw or "").replace("_link", "") in MAJOR]
        self.major_roads = sorted(major) or sorted(graph.road_ids)
        self.hospitals = sorted(hospitals, key=lambda h: h["id"])
        if not self.hospitals:
            raise ValueError("no hospitals available for the evaluation")

    def fingerprint(self) -> dict:
        g = self.graph
        h = hashlib.sha256()
        h.update(np.asarray(g.node_ids).tobytes())
        h.update(np.asarray(g.e_len).round(2).tobytes())
        h.update(json.dumps(self.hospitals, sort_keys=True, default=str).encode())
        return {"source": g.source, "nodes": int(len(g.node_ids)), "edges": int(len(g.e_from)), "roads": len(g.road_ids),
                "service_area_nodes": int(len(self.nodes)), "hospitals": len(self.hospitals), "sha256": h.hexdigest()[:16]}


def _pick(rng, options: dict) -> str:
    keys = list(options)
    p = np.array([options[k] if not isinstance(options[k], tuple) else options[k][0] for k in keys], dtype=float)
    return keys[int(rng.choice(len(keys), p=p / p.sum()))]


def _node(space: ScenarioSpace, rng) -> int:
    return int(space.nodes[int(rng.integers(len(space.nodes)))])


def _point(space: ScenarioSpace, i: int) -> tuple[float, float]:
    return round(float(space.graph.lat[i]), 6), round(float(space.graph.lon[i]), 6)


def generate_scenario(space: ScenarioSpace, seed: int, number: int, opt: ScenarioOptions) -> dict:
    rng = np.random.default_rng([int(seed), int(number)])
    g = space.graph
    D = opt.duration_s

    # ---- fleet
    n_fleet = int(rng.integers(opt.min_fleet, opt.max_fleet + 1))
    fleet = []
    for k in range(n_fleet):
        i = _node(space, rng)
        busy = bool(rng.random() < 0.2)
        fleet.append({"id": f"U{k + 1:02d}", "node": i, "lat": _point(space, i)[0], "lon": _point(space, i)[1],
                      "equipment_level": _pick(rng, EQUIPMENT_P), "fuel_level": round(float(rng.uniform(30, 100)), 1),
                      "missions_today": int(rng.integers(0, 5)),
                      "committed_until_s": round(float(rng.uniform(0.15, 0.8) * D), 1) if busy else None})

    # ---- simulated patients / calls
    n_inc = int(rng.integers(opt.min_incidents, opt.max_incidents + 1))
    rows = generate_patients(n=n_inc, seed=int(rng.integers(1, 2**31 - 1))).to_dict("records")
    times = sorted(float(t) for t in rng.uniform(0, D, n_inc))
    if n_inc >= 3 and rng.random() < 0.5:          # competing emergencies: a burst of near-simultaneous calls
        k0 = int(rng.integers(0, n_inc - 2))
        for j in range(1, 3):
            times[k0 + j] = times[k0] + float(rng.uniform(5, 60))
        times.sort()
    incidents = []
    for k, (t, row) in enumerate(zip(times, rows)):
        i = _node(space, rng)
        lat, lon = _point(space, i)
        true_case = case_from_row(row)
        uncertain = bool(rng.random() < UNCERTAIN_REPORT_SHARE)
        incidents.append({
            "id": f"S{number:04d}-I{k + 1:02d}", "t": round(t, 1), "node": i, "lat": lat, "lon": lon,
            "report_quality": "UNCERTAIN" if uncertain else "CLEAR",
            "case": noisy_report(true_case, rng) if uncertain else true_case, "true_case": true_case,
            "true_severity": row["severity"],
            "scene_time_s": round(opt.scene_time_s * float(rng.uniform(0.75, 1.5)), 1),
            "handover_time_s": round(opt.handover_time_s * float(rng.uniform(0.75, 1.5)), 1),
            "hospital_stay_s": round(float(rng.exponential(opt.ed_mean_stay_min * 60)), 1),
            "review_delay_s": round(float(rng.uniform(*opt.review_delay_s)), 1)})

    # ---- hospitals (real locations/capabilities, scenario load)
    regime_h = _pick(rng, {"LOW": 0.3, "MEDIUM": 0.4, "HIGH": 0.3})
    horizon = D + 4 * 3600
    hospitals = []
    for h in space.hospitals:
        cap = max(1, int(h["emergency_capacity"]))
        lo, hi = LOAD_REGIMES[regime_h]
        load = int(round(cap * float(rng.uniform(lo, hi))))
        rate_per_s = cap * float(rng.uniform(0.04, 0.12)) / 3600       # background arrivals
        n_bg = int(rng.poisson(rate_per_s * horizon))
        bg_t = np.sort(rng.uniform(-opt.ed_mean_stay_min * 60, horizon, n_bg))
        hospitals.append({
            "id": h["id"], "name": h["name"], "lat": h["lat"], "lon": h["lon"], "emergency_capacity": cap,
            "icu_available": int(rng.integers(0, max(1, int(h.get("icu_capacity") or 0)) + 1)) if h.get("icu_capacity") else 0,
            "trauma": bool(h["trauma"]), "cardiac": bool(h["cardiac"]), "stroke": bool(h["stroke"]),
            "initial_discharges_s": sorted(round(float(x), 1) for x in rng.exponential(opt.ed_mean_stay_min * 60, load)),
            "background": [[round(float(a), 1), round(float(rng.exponential(opt.ed_mean_stay_min * 60)), 1)]
                           for a in bg_t]})

    # ---- traffic regime on the major roads
    regime_t = _pick(rng, TRAFFIC_REGIMES)
    _, share, levels = TRAFFIC_REGIMES[regime_t]
    n_cong = int(round(share * len(space.major_roads)))
    initial_traffic: dict[str, list[str]] = {}
    if n_cong:
        for r in rng.choice(len(space.major_roads), n_cong, replace=False):
            lvl = levels[int(rng.integers(len(levels)))]
            initial_traffic.setdefault(lvl, []).append(space.major_roads[int(r)])
        initial_traffic = {k: sorted(v) for k, v in sorted(initial_traffic.items())}

    # ---- dynamic events
    events = []
    for inc in incidents:                      # disruptions on the likely approach route of a call
        if rng.random() >= 0.5:
            continue
        src = min(fleet, key=lambda u: (g.lat[u["node"]] - inc["lat"]) ** 2 + (g.lon[u["node"]] - inc["lon"]) ** 2)
        path, _ = g.shortest_path(src["node"], inc["node"], "free")
        roads = [g.road_ids[int(g.e_road[e])] for e in g.path_edges(path)] if len(path) > 3 else []
        if not roads:
            continue
        third = roads[len(roads) // 3: max(len(roads) // 3 + 1, 2 * len(roads) // 3)]
        road = third[int(rng.integers(len(third)))]
        t0 = inc["t"] + float(rng.uniform(20, 150))
        kind = _pick(rng, {"BLOCK": 0.4, "ACCIDENT": 0.3, "CONGESTION": 0.3})
        ev = {"t": round(t0, 1), "kind": kind, "road_id": road}
        if kind == "CONGESTION":
            ev["level"] = "SEVERE" if rng.random() < 0.6 else "HEAVY"
        events.append(ev)
        events.append({"t": round(t0 + float(rng.uniform(300, 1500)), 1), "kind": "CLEAR", "road_id": road})
    for _ in range(int(rng.integers(0, 4))):  # unrelated disruptions elsewhere
        road = space.major_roads[int(rng.integers(len(space.major_roads)))]
        t0 = float(rng.uniform(0, D))
        kind = _pick(rng, {"BLOCK": 0.3, "ACCIDENT": 0.3, "CONGESTION": 0.4})
        ev = {"t": round(t0, 1), "kind": kind, "road_id": road}
        if kind == "CONGESTION":
            ev["level"] = "SEVERE" if rng.random() < 0.5 else "HEAVY"
        events += [ev, {"t": round(t0 + float(rng.uniform(300, 1500)), 1), "kind": "CLEAR", "road_id": road}]
    if rng.random() < 0.3:                     # an ambulance becomes unavailable (breakdown)
        u = fleet[int(rng.integers(len(fleet)))]
        events.append({"t": round(float(rng.uniform(0, D)), 1), "kind": "AMBULANCE_UNAVAILABLE", "unit": u["id"],
                       "duration_s": round(float(rng.uniform(600, 1800)), 1)})
    if rng.random() < 0.3:                     # hospital surge (e.g. a mass-casualty walk-in)
        h = hospitals[int(rng.integers(len(hospitals)))]
        events.append({"t": round(float(rng.uniform(0, D)), 1), "kind": "HOSPITAL_SURGE", "hospital_id": h["id"],
                       "patients": int(rng.integers(3, 11))})
    events.sort(key=lambda e: (e["t"], e["kind"]))

    sc = {"number": number, "seed": int(seed), "start_hour": int(rng.integers(6, 23)), "duration_s": D,
          "traffic_regime": regime_t, "hospital_load_regime": regime_h, "fleet": fleet, "incidents": incidents,
          "hospitals": hospitals, "initial_traffic": initial_traffic, "events": events}
    sc["fingerprint"] = scenario_fingerprint(sc)
    return sc


def scenario_fingerprint(sc: dict) -> str:
    body = {k: v for k, v in sc.items() if k != "fingerprint"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=float).encode()).hexdigest()[:16]


def generate_scenarios(space: ScenarioSpace, seed: int, n: int, opt: ScenarioOptions | None = None) -> list[dict]:
    opt = opt or ScenarioOptions()
    return [generate_scenario(space, seed, k + 1, opt) for k in range(n)]


def summary(sc: dict) -> dict:
    sev: dict[str, int] = {}
    types: dict[str, int] = {}
    for i in sc["incidents"]:
        sev[i["true_severity"]] = sev.get(i["true_severity"], 0) + 1
        types[i["case"]["emergency_type"]] = types.get(i["case"]["emergency_type"], 0) + 1
    kinds: dict[str, int] = {}
    for e in sc["events"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return {"incidents": len(sc["incidents"]), "fleet": len(sc["fleet"]),
            "uncertain_reports": sum(1 for i in sc["incidents"] if i["report_quality"] == "UNCERTAIN"),
            "committed_units": sum(1 for u in sc["fleet"] if u["committed_until_s"]),
            "traffic_regime": sc["traffic_regime"], "hospital_load_regime": sc["hospital_load_regime"],
            "severities": sev, "emergency_types": types, "events": kinds}
