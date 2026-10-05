"""Client for a locally hosted OSRM server (http://project-osrm.org, BSD-2 licence)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("app.routing.osrm")


class OsrmUnavailable(RuntimeError):
    pass


@dataclass
class OsrmRoute:
    distance_m: float
    duration_s: float
    coords: list[tuple[float, float]]           # (lat, lon)
    node_ids: list[int]                          # OSM node ids along the route
    pair_distance_m: list[float]                 # per consecutive node pair
    pair_duration_s: list[float]


class OsrmClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = httpx.Client(timeout=timeout_s)
        self._last_ok: float | None = None
        self._last_error: str | None = None
        self._down_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def status(self) -> dict:
        return {"configured": self.enabled, "url": self.base_url or None, "last_ok": self._last_ok,
                "last_error": self._last_error}

    def health(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = self._client.get(f"{self.base_url}/nearest/v1/driving/0,0", timeout=1.5)
            ok = r.status_code in (200, 400)
            if ok:
                self._last_ok = time.time()
            return ok
        except httpx.HTTPError as exc:
            self._last_error = str(exc)[:200]
            return False

    def route(self, lat1: float, lon1: float, lat2: float, lon2: float, alternatives: int = 2) -> list[OsrmRoute]:
        if not self.enabled:
            raise OsrmUnavailable("OSRM_URL not configured")
        if time.time() < self._down_until:
            raise OsrmUnavailable(f"OSRM recently failed: {self._last_error}")
        url = (f"{self.base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
               f"?alternatives={alternatives}&overview=full&geometries=geojson&annotations=nodes,distance,duration")
        try:
            r = self._client.get(url)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._last_error = str(exc)[:200]
            self._down_until = time.time() + 15  # circuit breaker: do not hammer a dead server
            log.warning("osrm unavailable", extra={"event": "OSRM_UNAVAILABLE", "fields": {"error": self._last_error}})
            raise OsrmUnavailable(self._last_error) from exc
        if data.get("code") != "Ok":
            raise OsrmUnavailable(f"OSRM error: {data.get('code')} {data.get('message', '')}")
        self._last_ok = time.time()
        routes = []
        for rt in data.get("routes", []):
            leg = rt["legs"][0]
            ann = leg.get("annotation", {})
            routes.append(OsrmRoute(
                distance_m=float(rt["distance"]),
                duration_s=float(rt["duration"]),
                coords=[(c[1], c[0]) for c in rt["geometry"]["coordinates"]],
                node_ids=[int(n) for n in ann.get("nodes", [])],
                pair_distance_m=[float(d) for d in ann.get("distance", [])],
                pair_duration_s=[float(d) for d in ann.get("duration", [])],
            ))
        return routes
