"""Decision-strategy configurations: five progressive systems (A-E) and FULL-minus-one ablations.

A strategy only switches capabilities of the existing system on or off; it never re-implements them.

  capability            what it enables (all implemented by existing production code)
  severity              ML + rule triage drives the priority queue (PriorityScore), the required ambulance
                        capability and the hospital capability requirements. Off: FIFO queue, no triage.
  traffic               live-traffic routing (RoutingEngine default policy) and DispatchScore ranking on
                        traffic-adjusted ETAs. Off: static routing (free-flow time, closures avoided) and
                        nearest-unit selection by road distance.
  traffic_prediction    predicted traffic (TrafficModel MODEL or FALLBACK) in route selection and ETAs.
  hospital_intelligence hospital congestion forecast + HospitalScore (time to treatment, predicted load).
                        Off: fastest (or nearest, without traffic) hospital meeting the requirements.
  confidence            confidence-aware decision modes; HUMAN_REVIEW cases wait for a (simulated) review.
                        Off: every case is dispatched automatically on the final severity.
  reallocation          resource-conflict detection and priority-safe reallocation / escalation.
  dynamic_rerouting     telemetry-driven route monitoring and re-route rules (margin, cooldown, oscillation).
                        Off: a crew re-plans only when it physically reaches a closed road (reactive detour).
  explainability        per-decision explanation and counterfactuals (does not change any decision).
  batch_optimisation    OR-Tools assignment when several incidents wait at once (part of the live dispatcher).
                        Off: greedy assignment in queue order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

CAPABILITIES = ("severity", "traffic", "traffic_prediction", "hospital_intelligence", "confidence",
                "reallocation", "dynamic_rerouting", "explainability", "batch_optimisation")


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    severity: bool = False
    traffic: bool = False
    traffic_prediction: bool = False
    hospital_intelligence: bool = False
    confidence: bool = False
    reallocation: bool = False
    dynamic_rerouting: bool = False
    explainability: bool = False
    batch_optimisation: bool = False

    def flags(self) -> dict:
        d = asdict(self)
        d.pop("name")
        return d

    def without(self, capability: str) -> "StrategyConfig":
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {capability!r}")
        return replace(self, name=f"FULL-{ABLATION_LABEL[capability]}", **{capability: False})


BASELINE = StrategyConfig("BASELINE")
SEVERITY = replace(BASELINE, name="SEVERITY", severity=True)
TRAFFIC = replace(SEVERITY, name="TRAFFIC", traffic=True, traffic_prediction=True)
HOSPITAL = replace(TRAFFIC, name="HOSPITAL", hospital_intelligence=True)
FULL = replace(HOSPITAL, name="FULL", confidence=True, reallocation=True, dynamic_rerouting=True,
               explainability=True, batch_optimisation=True)

PROGRESSIVE = {s.name: s for s in (BASELINE, SEVERITY, TRAFFIC, HOSPITAL, FULL)}

# FULL minus one capability (the six requested ablations)
ABLATION_LABEL = {"confidence": "CONFIDENCE", "traffic_prediction": "TRAFFIC", "hospital_intelligence": "HOSPITAL",
                  "reallocation": "RESOURCE_REALLOCATION", "dynamic_rerouting": "DYNAMIC_REROUTING",
                  "explainability": "EXPLAINABILITY", "severity": "SEVERITY", "traffic": "LIVE_TRAFFIC",
                  "batch_optimisation": "BATCH_OPTIMISATION"}
ABLATION_CAPABILITIES = ("confidence", "traffic_prediction", "hospital_intelligence", "reallocation",
                         "dynamic_rerouting", "explainability")
ABLATIONS = {FULL.without(c).name: FULL.without(c) for c in ABLATION_CAPABILITIES}

ALL = {**PROGRESSIVE, **ABLATIONS}


def resolve(names: list[str]) -> list[StrategyConfig]:
    out = []
    for n in names:
        key = n.strip().upper().replace(" ", "")
        if key not in ALL:
            raise ValueError(f"unknown strategy {n!r}; choose from {', '.join(ALL)}")
        out.append(ALL[key])
    return out
