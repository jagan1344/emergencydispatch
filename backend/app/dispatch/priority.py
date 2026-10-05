"""Incident priority score and priority queue.

PriorityScore = 100 × ( 0.50·Severity + 0.20·TimeWaiting + 0.15·DistanceToPatient + 0.15·ResourceUrgency )

  Severity          = 0.5·level_value + 0.5·rule_score/100   (level_value LOW .25, MEDIUM .5, HIGH .75, CRITICAL 1)
  TimeWaiting       = min(1, waiting_sim_seconds / 600)       (10 simulated minutes saturates)
  DistanceToPatient = min(1, nearest_available_ambulance_m / 10 000)   (far patients need earlier dispatch)
  ResourceUrgency   = 1 − available_capable_units / total_capable_units (scarcity of the required unit type)
Higher score = dispatched first.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

LEVEL_VALUE = {"LOW": 0.25, "MEDIUM": 0.5, "HIGH": 0.75, "CRITICAL": 1.0}
WEIGHTS = {"severity": 0.50, "waiting": 0.20, "distance": 0.15, "resource": 0.15}
WAIT_SATURATION_S = 600.0
DISTANCE_SATURATION_M = 10_000.0


def priority_score(severity: str, rule_score: float, waiting_s: float, nearest_available_m: float | None,
                   available_capable: int, total_capable: int) -> tuple[float, dict]:
    sev = 0.5 * LEVEL_VALUE[severity] + 0.5 * max(0.0, min(100.0, rule_score)) / 100
    wait = min(1.0, max(0.0, waiting_s) / WAIT_SATURATION_S)
    dist = 1.0 if nearest_available_m is None else min(1.0, nearest_available_m / DISTANCE_SATURATION_M)
    res = 1.0 if total_capable <= 0 else 1.0 - min(1.0, available_capable / total_capable)
    comps = {"severity": round(sev, 4), "waiting": round(wait, 4), "distance": round(dist, 4),
             "resource": round(res, 4)}
    score = 100 * sum(WEIGHTS[k] * v for k, v in comps.items())
    return round(score, 2), comps


@dataclass(order=True)
class _Entry:
    neg_priority: float
    created_ts: float
    seq: int
    incident_id: str = field(compare=False)


class IncidentPriorityQueue:
    """Max-priority queue (ties broken by age, then insertion order). Lazy deletion on update."""

    def __init__(self):
        self._heap: list[_Entry] = []
        self._live: dict[str, _Entry] = {}
        self._seq = itertools.count()

    def push(self, incident_id: str, priority: float, created_ts: float) -> None:
        e = _Entry(-priority, created_ts, next(self._seq), incident_id)
        self._live[incident_id] = e
        heapq.heappush(self._heap, e)

    def remove(self, incident_id: str) -> None:
        self._live.pop(incident_id, None)

    def pop(self) -> tuple[str, float] | None:
        while self._heap:
            e = heapq.heappop(self._heap)
            if self._live.get(e.incident_id) is e:
                del self._live[e.incident_id]
                return e.incident_id, -e.neg_priority
        return None

    def ordered(self) -> list[tuple[str, float]]:
        return [(e.incident_id, -e.neg_priority) for e in sorted(self._live.values())]

    def __len__(self) -> int:
        return len(self._live)
