"""ETA and route-quality formulas."""
from __future__ import annotations

import math


def eta_base_s(distance_m: float, speed_mps: float) -> float:
    """ETA_base = Distance / Speed."""
    if speed_mps <= 0:
        return math.inf
    return distance_m / speed_mps


def route_efficiency(shortest_possible_distance_m: float, actual_route_distance_m: float) -> float:
    """RouteEfficiency = ShortestPossibleDistance / ActualRouteDistance (1.0 = perfectly direct)."""
    if actual_route_distance_m <= 0:
        return 1.0
    return min(1.0, shortest_possible_distance_m / actual_route_distance_m)


def time_saved_s(old_eta_s: float, new_eta_s: float) -> float:
    """TimeSaved = OldETA − NewETA."""
    return old_eta_s - new_eta_s


def degradation(planned_s: float, current_s: float) -> float:
    """Relative ETA increase; 0.25 means the remaining ETA grew by 25%."""
    if planned_s <= 0:
        return 0.0
    if math.isinf(current_s):
        return math.inf
    return (current_s - planned_s) / planned_s
