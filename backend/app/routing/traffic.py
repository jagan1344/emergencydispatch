"""Traffic model: congestion levels -> speed factors -> traffic-adjusted travel time.

Units: distance in metres, speed in m/s, time in seconds (km/h only for display/storage).

    AdjustedSpeed      = SpeedLimit × CongestionFactor × IncidentMultiplier
    AdjustedTravelTime = Distance / AdjustedSpeed          (∞ when the road is blocked)
"""
from __future__ import annotations

import math

CONGESTION_LEVELS = ["FREE", "LIGHT", "MODERATE", "HEAVY", "SEVERE", "BLOCKED"]
CONGESTION_FACTORS: dict[str, float] = {
    "FREE": 1.00,
    "LIGHT": 0.85,
    "MODERATE": 0.70,
    "HEAVY": 0.50,
    "SEVERE": 0.30,
    "BLOCKED": 0.00,
}
# Simulated vehicle density (veh / m / lane) typical for each level; used only for display & the simulator.
LEVEL_DENSITY = {"FREE": 0.01, "LIGHT": 0.03, "MODERATE": 0.05, "HEAVY": 0.08, "SEVERE": 0.12, "BLOCKED": 0.15}


def congestion_factor(level: str, blocked: bool = False) -> float:
    if blocked:
        return 0.0
    try:
        return CONGESTION_FACTORS[level]
    except KeyError as exc:
        raise ValueError(f"unknown congestion level {level!r}") from exc


def adjusted_speed_mps(speed_limit_kph: float, level: str, incident_multiplier: float = 1.0,
                       blocked: bool = False) -> float:
    """AdjustedSpeed = BaseSpeed × CongestionFactor × IncidentMultiplier (m/s)."""
    if speed_limit_kph <= 0:
        raise ValueError("speed limit must be positive")
    if not 0 < incident_multiplier <= 1:
        raise ValueError("incident_multiplier must be in (0, 1]")
    return (speed_limit_kph / 3.6) * congestion_factor(level, blocked) * incident_multiplier


def travel_time_s(distance_m: float, speed_mps: float) -> float:
    """TravelTime = Distance / Speed. Returns +inf for a zero speed (blocked road)."""
    if distance_m < 0:
        raise ValueError("distance must be non-negative")
    if speed_mps <= 0:
        return math.inf if distance_m > 0 else 0.0
    return distance_m / speed_mps


def level_from_speed_ratio(ratio: float) -> str:
    """Classify an observed speed ratio (current/limit) into the nearest congestion level."""
    if ratio <= 0:
        return "BLOCKED"
    best = min((lvl for lvl in CONGESTION_LEVELS if lvl != "BLOCKED"),
               key=lambda lvl: abs(CONGESTION_FACTORS[lvl] - ratio))
    return best


def worse(level_a: str, level_b: str) -> bool:
    return CONGESTION_LEVELS.index(level_a) > CONGESTION_LEVELS.index(level_b)
