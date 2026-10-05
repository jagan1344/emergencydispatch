"""Geographic helper formulas (fallback only: PostGIS/OSRM are preferred for real work)."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres.

    a = sin²(Δφ/2) + cos φ1 · cos φ2 · sin²(Δλ/2)
    c = 2 · atan2(√a, √(1−a))
    d = R · c
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def kph_to_mps(kph: float) -> float:
    return kph / 3.6


def mps_to_kph(mps: float) -> float:
    return mps * 3.6


def interpolate(lat1: float, lon1: float, lat2: float, lon2: float, frac: float) -> tuple[float, float]:
    """Linear interpolation between two close points (adequate for road segments of < 1 km)."""
    frac = max(0.0, min(1.0, frac))
    return lat1 + (lat2 - lat1) * frac, lon1 + (lon2 - lon1) * frac


def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Point reached travelling distance_m from (lat, lon) along bearing (spherical earth)."""
    d = distance_m / EARTH_RADIUS_M
    br = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)
