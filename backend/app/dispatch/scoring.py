"""Ambulance dispatch score (lower = better) and hospital score (lower = better).

DispatchScore = 0.40·ETA + 0.20·Capability + 0.15·Traffic + 0.10·Workload + 0.10·Fuel + 0.05·Distance
  ETA        = eta / max candidate eta
  Capability = 1 − capability_match       (match 1.0 perfect, 0.5 acceptable, 0.0 unsuitable)
  Traffic    = traffic delay / max candidate traffic delay   (delay = adjusted − free-flow duration)
  Workload   = min(1, missions_today / 6)
  Fuel       = 1 − fuel_level/100
  Distance   = route distance / max candidate route distance

HospitalScore = 0.45·ETA + 0.25·CapabilityMismatch + 0.15·CapacityLoad + 0.15·TrafficDelay
"""
from __future__ import annotations

from dataclasses import dataclass, field

DISPATCH_WEIGHTS = {"eta": 0.40, "capability": 0.20, "traffic": 0.15, "workload": 0.10, "fuel": 0.10,
                    "distance": 0.05}
HOSPITAL_WEIGHTS = {"eta": 0.45, "capability": 0.25, "load": 0.15, "traffic": 0.15}
EQUIPMENT_RANK = {"BASIC": 0, "ADVANCED": 1, "ICU": 2}
# capability_match[required][equipment]
CAPABILITY_MATCH = {
    "ICU": {"ICU": 1.0, "ADVANCED": 0.5, "BASIC": 0.0},
    "ADVANCED": {"ADVANCED": 1.0, "ICU": 0.5, "BASIC": 0.0},
    "BASIC": {"BASIC": 1.0, "ADVANCED": 0.5, "ICU": 0.5},
}
MAX_MISSIONS = 6


def capability_match(required: str, equipment: str) -> float:
    return CAPABILITY_MATCH[required][equipment]


def _ratio(v: float, vmax: float) -> float:
    return 0.0 if vmax <= 0 else min(1.0, v / vmax)


@dataclass
class CandidateInput:
    ambulance_id: str
    equipment_level: str
    eta_s: float
    distance_m: float
    traffic_delay_s: float
    missions_today: int
    fuel_level: float
    extra: dict = field(default_factory=dict)


@dataclass
class ScoredCandidate:
    ambulance_id: str
    score: float
    components: dict[str, float]
    capability_match: float
    eta_s: float
    distance_m: float
    traffic_delay_s: float
    equipment_level: str
    suitable: bool
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ambulance_id": self.ambulance_id, "score": round(self.score, 4),
                "components": {k: round(v, 4) for k, v in self.components.items()},
                "capability_match": self.capability_match, "eta_s": round(self.eta_s, 1),
                "distance_m": round(self.distance_m, 1), "traffic_delay_s": round(self.traffic_delay_s, 1),
                "equipment_level": self.equipment_level, "suitable": self.suitable, **self.extra}


def score_candidates(cands: list[CandidateInput], required: str) -> list[ScoredCandidate]:
    """Score all candidates; returns them sorted best-first. Unsuitable units (match 0) are kept but
    flagged; the caller only uses them when no suitable unit exists."""
    if not cands:
        return []
    max_eta = max(c.eta_s for c in cands)
    max_dist = max(c.distance_m for c in cands)
    max_delay = max(c.traffic_delay_s for c in cands)
    out = []
    for c in cands:
        match = capability_match(required, c.equipment_level)
        comps = {
            "eta": _ratio(c.eta_s, max_eta),
            "capability": 1.0 - match,
            "traffic": _ratio(c.traffic_delay_s, max_delay),
            "workload": min(1.0, c.missions_today / MAX_MISSIONS),
            "fuel": 1.0 - max(0.0, min(100.0, c.fuel_level)) / 100.0,
            "distance": _ratio(c.distance_m, max_dist),
        }
        score = sum(DISPATCH_WEIGHTS[k] * v for k, v in comps.items())
        out.append(ScoredCandidate(c.ambulance_id, score, comps, match, c.eta_s, c.distance_m, c.traffic_delay_s,
                                   c.equipment_level, match > 0.0, dict(c.extra)))
    out.sort(key=lambda s: (not s.suitable, s.score, s.eta_s))
    return out


def traffic_label(delay_s: float, eta_s: float) -> str:
    if eta_s <= 0:
        return "Low"
    r = delay_s / eta_s
    return "Low" if r < 0.1 else "Moderate" if r < 0.3 else "High"


def explain_dispatch(ranked: list[ScoredCandidate]) -> str:
    best = ranked[0]
    lines = [
        f"Why was {best.ambulance_id} selected?",
        f"ETA: {best.eta_s / 60:.1f} min",
        f"Capability: {best.equipment_level} ({'perfect match' if best.capability_match == 1 else 'acceptable' if best.capability_match > 0 else 'UNSUITABLE - no suitable unit available'})",
        f"Traffic: {traffic_label(best.traffic_delay_s, best.eta_s)} (+{best.traffic_delay_s:.0f} s delay)",
        f"Workload: {'Low' if best.components['workload'] < 0.34 else 'Medium' if best.components['workload'] < 0.67 else 'High'}",
        f"Distance: {best.distance_m / 1000:.2f} km",
        f"Final Dispatch Score: {best.score:.3f}",
    ]
    if len(ranked) > 1:
        alt = ranked[1]
        lines.append(f"Alternative: {alt.ambulance_id} score {alt.score:.3f} (ETA {alt.eta_s / 60:.1f} min, {alt.equipment_level})")
        diffs = {k: alt.components[k] - best.components[k] for k in best.components}
        key = max(diffs, key=lambda k: diffs[k] * DISPATCH_WEIGHTS[k])
        lines.append(f"Therefore {best.ambulance_id} selected (largest advantage: {key}).")
    else:
        lines.append(f"Therefore {best.ambulance_id} selected (only available candidate).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- hospitals
def hospital_requirements(severity: str, emergency_type: str) -> list[str]:
    req = []
    if severity == "CRITICAL":
        req.append("icu")
    if emergency_type in ("accident", "trauma", "fire") and severity in ("HIGH", "CRITICAL"):
        req.append("trauma")
    if emergency_type == "cardiac":
        req.append("cardiac")
    if emergency_type == "stroke":
        req.append("stroke")
    return req


@dataclass
class HospitalInput:
    hospital_id: str
    name: str
    eta_s: float
    traffic_delay_s: float
    distance_m: float
    current_load: int
    emergency_capacity: int
    icu_available: int
    trauma: bool
    cardiac: bool
    stroke: bool


def hospital_capabilities(h: HospitalInput) -> dict[str, bool]:
    return {"icu": h.icu_available > 0, "trauma": h.trauma, "cardiac": h.cardiac, "stroke": h.stroke}


def score_hospitals(hs: list[HospitalInput], requirements: list[str]) -> list[dict]:
    if not hs:
        return []
    max_eta = max(h.eta_s for h in hs)
    max_delay = max(h.traffic_delay_s for h in hs)
    out = []
    for h in hs:
        caps = hospital_capabilities(h)
        missing = [r for r in requirements if not caps[r]]
        comps = {
            "eta": _ratio(h.eta_s, max_eta),
            "capability": (len(missing) / len(requirements)) if requirements else 0.0,
            "load": min(1.0, h.current_load / h.emergency_capacity),
            "traffic": _ratio(h.traffic_delay_s, max_delay),
        }
        score = sum(HOSPITAL_WEIGHTS[k] * v for k, v in comps.items())
        out.append({"hospital_id": h.hospital_id, "name": h.name, "score": round(score, 4),
                    "components": {k: round(v, 4) for k, v in comps.items()}, "eta_s": round(h.eta_s, 1),
                    "distance_m": round(h.distance_m, 1), "traffic_delay_s": round(h.traffic_delay_s, 1),
                    "missing_capabilities": missing, "full": h.current_load >= h.emergency_capacity})
    out.sort(key=lambda d: (d["full"], d["score"]))
    return out


def explain_hospital(ranked: list[dict], requirements: list[str]) -> str:
    best = ranked[0]
    lines = [f"Selected {best['name']} ({best['hospital_id']}): ETA {best['eta_s'] / 60:.1f} min, "
             f"{best['distance_m'] / 1000:.2f} km, load {best['components']['load'] * 100:.0f}%, score {best['score']:.3f}."]
    lines.append("Patient requires: " + (", ".join(requirements) if requirements else "general emergency care") +
                 (". All requirements met." if not best["missing_capabilities"]
                  else f". WARNING - missing at selected hospital: {', '.join(best['missing_capabilities'])}"))
    closer = [h for h in ranked[1:] if h["distance_m"] < best["distance_m"]]
    if closer:
        c = min(closer, key=lambda h: h["distance_m"])
        why = []
        if c["missing_capabilities"]:
            why.append("lacks " + ", ".join(c["missing_capabilities"]))
        if c["components"]["load"] > best["components"]["load"]:
            why.append(f"higher load ({c['components']['load'] * 100:.0f}%)")
        if c["eta_s"] > best["eta_s"]:
            why.append("slower in current traffic")
        lines.append(f"Closer option {c['name']} ({c['distance_m'] / 1000:.2f} km) not chosen: "
                     f"{', '.join(why) or 'higher overall score'} (score {c['score']:.3f}).")
    return " ".join(lines)
