"""Global multi-incident assignment with Google OR-Tools CP-SAT.

When several incidents are waiting at once, greedy one-by-one selection can give the best unit to a
lower-priority incident. We instead solve

    maximise  Σ_ij x_ij · ( PRIORITY_WEIGHT · priority_i − 100 · dispatchScore_ij )
    s.t.      Σ_j x_ij ≤ 1  (each incident gets at most one ambulance)
              Σ_i x_ij ≤ 1  (each ambulance serves at most one incident)
              x_ij = 0 for unsuitable pairs (capability match 0) unless the incident has no suitable unit

so that, when units are scarce, high-priority incidents are served first and, among those, the
assignment with the lowest total dispatch score is chosen.
"""
from __future__ import annotations

from ortools.sat.python import cp_model

PRIORITY_WEIGHT = 10


def optimal_assignment(priorities: dict[str, float], scores: dict[str, dict[str, tuple[float, bool]]],
                       time_limit_s: float = 2.0) -> dict[str, str]:
    """priorities: incident -> priority (0..100); scores: incident -> ambulance -> (dispatch score, suitable).
    Returns incident -> ambulance."""
    model = cp_model.CpModel()
    x: dict[tuple[str, str], cp_model.IntVar] = {}
    obj = []
    for inc, amb_scores in scores.items():
        any_suitable = any(s for _, s in amb_scores.values())
        for amb, (score, suitable) in amb_scores.items():
            if any_suitable and not suitable:
                continue
            var = model.NewBoolVar(f"x_{inc}_{amb}")
            x[(inc, amb)] = var
            value = int(round(PRIORITY_WEIGHT * priorities[inc] * 10 - 100 * score * 10))
            obj.append(value * var)
    incidents = {i for i, _ in x}
    ambulances = {a for _, a in x}
    for i in incidents:
        model.Add(sum(v for (ii, _), v in x.items() if ii == i) <= 1)
    for a in ambulances:
        model.Add(sum(v for (_, aa), v in x.items() if aa == a) <= 1)
    model.Maximize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {}
    return {i: a for (i, a), v in x.items() if solver.Value(v)}
