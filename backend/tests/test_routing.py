"""Graph routing + traffic re-costing + optimizer on small hand-built networks."""
import math

import pytest

from app.dispatch.optimizer import optimal_assignment
from app.routing.engine import NoRouteError, RoutingEngine
from app.routing.graph import RoadGraph


def diamond() -> RoadGraph:
    """   1 --R1(fast, 600m)-- 2
         /                     \\
        0                       3        A->B via top (R1) or bottom (R2, slower but longer)
         \\                     /
          4 --R2(slow, 900m)-- 5
    """
    lat = [0.0, 0.003, 0.003, 0.0, -0.003, -0.003]
    lon = [0.0, 0.002, 0.008, 0.010, 0.002, 0.008]
    edges = [(0, 1, 250, 0), (1, 2, 600, 1), (2, 3, 250, 0), (0, 4, 250, 0), (4, 5, 900, 2), (5, 3, 250, 0)]
    e_from = [a for a, b, _, _ in edges] + [b for a, b, _, _ in edges]
    e_to = [b for a, b, _, _ in edges] + [a for a, b, _, _ in edges]
    e_len = [d for _, _, d, _ in edges] * 2
    e_road = [r for _, _, _, r in edges] * 2
    return RoadGraph([10, 11, 12, 13, 14, 15], lat, lon, e_from, e_to, e_len, e_road,
                     ["R0", "R1", "R2"], ["Link", "Top", "Bottom"], [36, 36, 36], ["primary"] * 3, "test")


def test_route_selection_prefers_fastest_and_reacts_to_traffic():
    g = diamond()
    eng = RoutingEngine(g)
    r = eng.route((0.0, 0.0), (0.0, 0.010))
    assert "R1" in r.road_ids()
    assert r.distance_m == pytest.approx(1100, abs=1)
    assert r.adjusted_duration_s == pytest.approx(110, abs=0.5)        # 1100 m at 10 m/s
    g.set_road_state("R1", "SEVERE", 1.0, False)                        # top: 600 m at 3 m/s = 200 s
    r2 = eng.route((0.0, 0.0), (0.0, 0.010))
    assert "R2" in r2.road_ids()                                         # bottom: 1400 m at 10 m/s = 140 s
    assert r2.adjusted_duration_s == pytest.approx(140, abs=0.5)
    assert r2.base_duration_s == pytest.approx(140, abs=0.5)
    assert r2.shortest_distance_m == pytest.approx(1100, abs=1)          # shortest possible ignores traffic
    g.set_road_state("R1", "FREE", 1.0, False)
    g.set_road_state("R2", "BLOCKED", 1.0, True)
    g.set_road_state("R1", "BLOCKED", 1.0, True)
    with pytest.raises(NoRouteError):
        eng.route((0.0, 0.0), (0.0, 0.010))


def test_remaining_eta_and_recost():
    g = diamond()
    eng = RoutingEngine(g)
    r = eng.route((0.0, 0.0), (0.0, 0.010))
    assert eng.remaining_eta(r.segments, 0) == pytest.approx(110, abs=0.5)
    assert eng.remaining_eta(r.segments, 550) == pytest.approx(55, abs=0.5)
    g.set_road_state("R1", "HEAVY", 1.0, False)
    assert eng.remaining_eta(r.segments, 0) == pytest.approx(50 + 120, abs=0.5)
    assert eng.remaining_eta(r.segments, 0, use_planned=True) == pytest.approx(110, abs=0.5)
    assert eng.remaining_eta(r.segments, 10_000) == 0


def test_blocked_edge_is_infinite_weight():
    g = diamond()
    g.set_road_state("R2", "BLOCKED", 1.0, True)
    w = g.edge_weights("time")
    assert math.isinf(w[4]) and math.isfinite(w[1])


def test_ortools_assignment_serves_high_priority_first():
    # one ambulance, two incidents: the critical one must get it even if the other scores better
    res = optimal_assignment({"crit": 90, "low": 20}, {"crit": {"A": (0.6, True)}, "low": {"A": (0.1, True)}})
    assert res == {"crit": "A"}
    # two ambulances: global optimum, not greedy
    res = optimal_assignment({"i1": 80, "i2": 79},
                             {"i1": {"a": (0.10, True), "b": (0.20, True)}, "i2": {"a": (0.15, True), "b": (0.90, True)}})
    assert res == {"i1": "b", "i2": "a"}
    # unsuitable pairs excluded when a suitable unit exists
    assert optimal_assignment({"i": 50}, {"i": {"basic": (0.0, False), "icu": (0.9, True)}}) == {"i": "icu"}
