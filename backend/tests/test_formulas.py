"""Unit tests for the formulas: Haversine, traffic adjustment, ETA, severity, priority, scores."""
import math

import pytest

from app.dispatch.priority import IncidentPriorityQueue, priority_score
from app.dispatch.scoring import (CandidateInput, HospitalInput, capability_match, hospital_requirements,
                                  score_candidates, score_hospitals)
from app.dispatch.severity import combine_severity, level_for_score, required_capability, severity_score
from app.routing.eta import degradation, route_efficiency, time_saved_s
from app.routing.traffic import adjusted_speed_mps, congestion_factor, travel_time_s
from app.utils.geo import haversine_m
from tests.conftest import CRITICAL_CASE, MILD_CASE


def test_haversine_known_distance():
    # Bengaluru MG Road -> Kempegowda airport is ~ 30 km great-circle
    d = haversine_m(12.9756, 77.6050, 13.1986, 77.7066)
    assert 26_000 < d < 28_000
    assert haversine_m(10, 10, 10, 10) == 0
    # 1 degree of latitude ~ 111.2 km
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)


def test_congestion_factors_and_adjusted_speed():
    assert congestion_factor("FREE") == 1.0
    assert congestion_factor("HEAVY") == 0.5
    assert congestion_factor("FREE", blocked=True) == 0.0
    # 36 km/h = 10 m/s ; MODERATE 0.7 ; incident multiplier 0.5 -> 3.5 m/s
    assert adjusted_speed_mps(36, "MODERATE", 0.5) == pytest.approx(3.5)
    with pytest.raises(ValueError):
        congestion_factor("JAMMED")


def test_travel_time_and_eta():
    assert travel_time_s(1000, 10) == 100
    assert math.isinf(travel_time_s(1000, 0))
    assert travel_time_s(0, 0) == 0
    # traffic: 1 km at 36 km/h FREE = 100 s, SEVERE (0.3) = 333.3 s
    assert travel_time_s(1000, adjusted_speed_mps(36, "SEVERE")) == pytest.approx(333.33, rel=1e-3)


def test_route_formulas():
    assert route_efficiency(900, 1000) == pytest.approx(0.9)
    assert time_saved_s(13 * 60, 9 * 60) == 240
    assert degradation(100, 125) == pytest.approx(0.25)
    assert math.isinf(degradation(100, math.inf))


def test_severity_score_levels_and_reasons():
    crit = severity_score(CRITICAL_CASE)
    assert crit.level == "CRITICAL" and crit.score > 75
    assert "loss of consciousness" in crit.reasons and "breathing difficulty" in crit.reasons
    mild = severity_score(MILD_CASE)
    assert mild.level == "LOW" and mild.score <= 25
    assert all(0 <= v <= 1 for v in crit.components.values())
    assert [level_for_score(x) for x in (0, 25, 26, 50, 51, 75, 76, 100)] == \
        ["LOW", "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH", "CRITICAL", "CRITICAL"]


def test_combine_severity_safety_override():
    assert combine_severity("HIGH", "MEDIUM") == ("HIGH", "ML")
    final, basis = combine_severity("LOW", "HIGH")
    assert final == "HIGH" and basis.startswith("SAFETY_OVERRIDE")
    assert combine_severity(None, "MEDIUM")[0] == "MEDIUM"
    assert required_capability("CRITICAL", "accident") == "ICU"
    assert required_capability("MEDIUM", "cardiac") == "ADVANCED"


def test_priority_score_and_queue():
    hi, comps = priority_score("CRITICAL", 90, 0, 3000, 1, 4)
    lo, _ = priority_score("LOW", 10, 0, 3000, 1, 4)
    assert hi > lo and 0 <= lo <= 100 and 0 <= hi <= 100
    waited, _ = priority_score("LOW", 10, 600, 3000, 1, 4)
    assert waited == pytest.approx(lo + 20, abs=0.01)   # 0.20 weight x waiting saturates at 600 s
    assert comps["resource"] == pytest.approx(0.75)
    q = IncidentPriorityQueue()
    q.push("a", 30, 1)
    q.push("b", 80, 2)
    q.push("c", 80, 0)
    q.push("a", 90, 1)        # update
    assert [q.pop()[0] for _ in range(3)] == ["a", "c", "b"]
    assert q.pop() is None


def test_dispatch_score_prefers_balanced_candidate_not_nearest():
    cands = [
        CandidateInput("NEAR-BASIC", "BASIC", eta_s=200, distance_m=1500, traffic_delay_s=10, missions_today=0, fuel_level=90),
        CandidateInput("ICU-FAR", "ICU", eta_s=300, distance_m=2400, traffic_delay_s=20, missions_today=1, fuel_level=80),
        CandidateInput("ICU-TIRED", "ICU", eta_s=320, distance_m=2500, traffic_delay_s=100, missions_today=6, fuel_level=20),
    ]
    ranked = score_candidates(cands, "ICU")
    assert ranked[0].ambulance_id == "ICU-FAR"
    near = next(r for r in ranked if r.ambulance_id == "NEAR-BASIC")
    assert not near.suitable and ranked[-1] is near          # unsuitable units rank last
    best = ranked[0]
    expected = 0.40 * (300 / 320) + 0.20 * 0 + 0.15 * (20 / 100) + 0.10 * (1 / 6) + 0.10 * 0.2 + 0.05 * (2400 / 2500)
    assert best.score == pytest.approx(expected)
    assert capability_match("ICU", "ADVANCED") == 0.5 and capability_match("BASIC", "ICU") == 0.5


def test_candidate_filtering_unsuitable_only_when_needed():
    ranked = score_candidates([CandidateInput("B1", "BASIC", 100, 800, 0, 0, 100)], "ICU")
    assert len(ranked) == 1 and not ranked[0].suitable and ranked[0].components["capability"] == 1.0


def test_hospital_score_capability_beats_proximity():
    reqs = hospital_requirements("CRITICAL", "cardiac")
    assert set(reqs) == {"icu", "cardiac"}
    hs = [
        HospitalInput("A", "Near no ICU", eta_s=240, traffic_delay_s=0, distance_m=4000, current_load=5,
                      emergency_capacity=20, icu_available=0, trauma=False, cardiac=False, stroke=False),
        HospitalInput("B", "Far with ICU", eta_s=360, traffic_delay_s=0, distance_m=6000, current_load=5,
                      emergency_capacity=20, icu_available=3, trauma=False, cardiac=True, stroke=False),
    ]
    ranked = score_hospitals(hs, reqs)
    assert ranked[0]["hospital_id"] == "B"
    assert ranked[1]["missing_capabilities"] == ["icu", "cardiac"]
    assert ranked[0]["score"] == pytest.approx(0.45 * 1.0 + 0.25 * 0 + 0.15 * 0.25 + 0.15 * 0)
