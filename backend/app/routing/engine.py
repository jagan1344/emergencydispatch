"""Traffic-aware routing engine.

Flow:  point → nearest road node → candidate routes → traffic re-costing → best adjusted ETA.

Candidate routes come from two engines:
  1. 'graph' : Dijkstra on the in-memory road graph weighted by *current* traffic-adjusted travel time.
               Blocked roads are removed, so this candidate always respects closures.
  2. 'osrm'  : up to 3 alternatives from the local OSRM server (OpenStreetMap car profile). OSRM does
               not know our live traffic, so every OSRM route is re-costed edge by edge with the live
               road conditions (OSM node pairs are mapped onto our road edges).
The candidate with the lowest traffic-adjusted duration is selected.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field

from app.routing.graph import RoadGraph
from app.routing.osrm_client import OsrmClient, OsrmUnavailable
from app.utils.geo import haversine_m

log = logging.getLogger("app.routing.engine")

ACCESS_SPEED_KPH = 20.0       # speed on the short off-network connector between a point and its road node
MAX_SNAP_M = 1500.0           # points farther than this from any road are rejected


class NoRouteError(RuntimeError):
    pass


@dataclass
class Segment:
    road_id: str | None
    from_node: int | None
    to_node: int | None
    length_m: float
    base_speed_kph: float
    adj_speed_kph: float          # 0 when blocked
    coords: list[tuple[float, float]]  # (lat, lon) start..end

    @property
    def base_time_s(self) -> float:
        return self.length_m / (self.base_speed_kph / 3.6) if self.base_speed_kph > 0 else math.inf

    @property
    def adj_time_s(self) -> float:
        if self.length_m == 0:
            return 0.0
        return self.length_m / (self.adj_speed_kph / 3.6) if self.adj_speed_kph > 0 else math.inf


@dataclass
class RouteResult:
    engine: str
    network_source: str
    segments: list[Segment]
    distance_m: float
    base_duration_s: float
    adjusted_duration_s: float
    osrm_duration_s: float | None = None
    shortest_distance_m: float | None = None
    origin_snap_m: float = 0.0
    dest_snap_m: float = 0.0
    alternatives: list[dict] = field(default_factory=list)

    @property
    def coords(self) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for s in self.segments:
            for c in s.coords:
                if not out or out[-1] != c:
                    out.append(c)
        return out

    @property
    def feasible(self) -> bool:
        return math.isfinite(self.adjusted_duration_s)

    @property
    def traffic_delay_s(self) -> float:
        return max(0.0, self.adjusted_duration_s - self.base_duration_s) if self.feasible else math.inf

    def road_ids(self) -> set[str]:
        return {s.road_id for s in self.segments if s.road_id}

    def summary(self) -> dict:
        return {"engine": self.engine, "distance_m": round(self.distance_m, 1),
                "base_duration_s": round(float(self.base_duration_s), 1),
                "adjusted_duration_s": round(float(self.adjusted_duration_s), 1) if self.feasible else None,
                "osrm_duration_s": round(self.osrm_duration_s, 1) if self.osrm_duration_s else None,
                "feasible": self.feasible}


def _totals(segments: list[Segment]) -> tuple[float, float, float]:
    dist = sum(s.length_m for s in segments)
    base = sum(s.base_time_s for s in segments)
    adj = sum(s.adj_time_s for s in segments)
    return dist, base, adj


class RoutingEngine:
    def __init__(self, graph: RoadGraph, osrm: OsrmClient | None = None):
        self.graph = graph
        self.osrm = osrm

    # -------------------------------------------------------------- segment builders
    def _edge_segment(self, e: int) -> Segment:
        g = self.graph
        u, v, r = int(g.e_from[e]), int(g.e_to[e]), int(g.e_road[e])
        return Segment(
            road_id=g.road_ids[r], from_node=int(g.node_ids[u]), to_node=int(g.node_ids[v]),
            length_m=float(g.e_len[e]), base_speed_kph=float(g.road_limit_kph[r]),
            adj_speed_kph=g.road_speed_mps(r) * 3.6,
            coords=[(float(g.lat[u]), float(g.lon[u])), (float(g.lat[v]), float(g.lon[v]))],
        )

    @staticmethod
    def _access_segment(a: tuple[float, float], b: tuple[float, float]) -> Segment | None:
        d = haversine_m(a[0], a[1], b[0], b[1])
        if d < 1.0:
            return None
        return Segment(None, None, None, d, ACCESS_SPEED_KPH, ACCESS_SPEED_KPH, [a, b])

    def recost(self, segments: list[Segment]) -> list[Segment]:
        """Return the same segments with adj_speed_kph refreshed from the current traffic state."""
        g = self.graph
        out = []
        for s in segments:
            if s.road_id and s.road_id in g.road_idx:
                r = g.road_idx[s.road_id]
                s = Segment(s.road_id, s.from_node, s.to_node, s.length_m, s.base_speed_kph,
                            g.road_speed_mps(r) * 3.6, s.coords)
            out.append(s)
        return out

    # -------------------------------------------------------------- candidates
    def _graph_candidate(self, o_idx: int, d_idx: int) -> list[Segment] | None:
        path, cost = self.graph.shortest_path(o_idx, d_idx, "time")
        if not path:
            return None
        return [self._edge_segment(e) for e in self.graph.path_edges(path)]

    def _osrm_candidates(self, origin, dest) -> list[tuple[list[Segment], float]]:
        if not self.osrm or not self.osrm.enabled:
            return []
        try:
            routes = self.osrm.route(origin[0], origin[1], dest[0], dest[1], alternatives=3)
        except OsrmUnavailable:
            return []
        g = self.graph
        out = []
        for rt in routes:
            segs: list[Segment] = []
            nodes = rt.node_ids
            for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
                ia, ib = g.idx_of.get(a), g.idx_of.get(b)
                e = g.pair_edge.get((ia, ib)) if ia is not None and ib is not None else None
                if e is not None:
                    segs.append(self._edge_segment(e))
                else:
                    # pair outside our imported graph: fall back to OSRM's own free-flow estimate (no traffic data)
                    d = rt.pair_distance_m[k] if k < len(rt.pair_distance_m) else 0.0
                    t = rt.pair_duration_s[k] if k < len(rt.pair_duration_s) else 0.0
                    spd = (d / t * 3.6) if t > 0 else ACCESS_SPEED_KPH
                    ca = (float(g.lat[ia]), float(g.lon[ia])) if ia is not None else None
                    cb = (float(g.lat[ib]), float(g.lon[ib])) if ib is not None else None
                    coords = [c for c in (ca, cb) if c] or []
                    segs.append(Segment(None, a, b, d, spd, spd, coords))
            if segs:
                out.append((segs, rt.duration_s))
        return out

    # -------------------------------------------------------------- public API
    def route(self, origin: tuple[float, float], dest: tuple[float, float], *, origin_node: int | None = None,
              compute_shortest: bool = True, prefix: list[Segment] | None = None) -> RouteResult:
        """Best traffic-aware route from origin to dest (lat, lon tuples).

        origin_node: start from this graph node index instead of snapping origin (used for re-routing);
        prefix: segments prepended verbatim (e.g. the remainder of the edge the ambulance is on).
        """
        g = self.graph
        with g.lock:
            if origin_node is None:
                o_idx, o_snap = g.nearest_node(*origin)
            else:
                o_idx, o_snap = origin_node, 0.0
            d_idx, d_snap = g.nearest_node(*dest)
            if o_snap > MAX_SNAP_M or d_snap > MAX_SNAP_M:
                raise NoRouteError(f"point is {max(o_snap, d_snap):.0f} m from the nearest road (max {MAX_SNAP_M:.0f} m)")
            o_pt = (float(g.lat[o_idx]), float(g.lon[o_idx]))
            d_pt = (float(g.lat[d_idx]), float(g.lon[d_idx]))

            head: list[Segment] = list(prefix or [])
            if origin_node is None:
                acc = self._access_segment(origin, o_pt)
                if acc:
                    head.append(acc)
            tail: list[Segment] = []
            acc = self._access_segment(d_pt, dest)
            if acc:
                tail.append(acc)

            candidates: list[tuple[str, list[Segment], float | None]] = []
            graph_core = self._graph_candidate(o_idx, d_idx)
            if graph_core is not None:
                candidates.append(("graph", graph_core, None))
            if origin_node is None:
                for segs, osrm_dur in self._osrm_candidates(origin, dest):
                    candidates.append(("osrm", segs, osrm_dur))
            else:
                for segs, osrm_dur in self._osrm_candidates(o_pt, dest):
                    candidates.append(("osrm", segs, osrm_dur))

            results: list[RouteResult] = []
            for engine, core, osrm_dur in candidates:
                if engine == "osrm":
                    segs = head + core
                    # OSRM snaps the destination itself; add our access leg only if its end is far from dest
                    if core and core[-1].coords and haversine_m(*core[-1].coords[-1], *dest) > 5:
                        segs += tail
                else:
                    segs = head + core + tail
                dist, base, adj = _totals(segs)
                results.append(RouteResult(engine, g.source, segs, dist, base, adj, osrm_dur,
                                           origin_snap_m=o_snap, dest_snap_m=d_snap))
            if not results:
                raise NoRouteError("destination unreachable on the current road network")
            feasible = [r for r in results if r.feasible]
            if not feasible:
                raise NoRouteError("all candidate routes are blocked")
            best = min(feasible, key=lambda r: (r.adjusted_duration_s, r.distance_m))
            best.alternatives = [dict(r.summary(), selected=r is best) for r in results]
            if compute_shortest:
                path, length = g.shortest_path(o_idx, d_idx, "length")
                extra = sum(s.length_m for s in head) + sum(s.length_m for s in tail)
                best.shortest_distance_m = (length + extra) if path else None
            return best

    def shortest_distance(self, origin: tuple[float, float], dest: tuple[float, float]) -> float | None:
        """Shortest possible network distance (ignores traffic and closures) incl. access legs."""
        g = self.graph
        with g.lock:
            o, o_snap = g.nearest_node(*origin)
            d, d_snap = g.nearest_node(*dest)
            path, length = g.shortest_path(o, d, "length")
        return (length + o_snap + d_snap) if path else None

    def remaining(self, segments: list[Segment], progress_m: float) -> tuple[list[Segment], float]:
        """Segments not yet completed at progress_m, plus the fraction left of the first one."""
        cum = 0.0
        for k, s in enumerate(segments):
            if cum + s.length_m > progress_m:
                frac_left = (cum + s.length_m - progress_m) / s.length_m if s.length_m > 0 else 0.0
                return segments[k:], frac_left
            cum += s.length_m
        return [], 0.0

    def remaining_eta(self, segments: list[Segment], progress_m: float, use_planned: bool = False) -> float:
        rem, frac = self.remaining(segments, progress_m)
        if not rem:
            return 0.0
        segs = rem if use_planned else self.recost(rem)
        total = segs[0].adj_time_s * frac
        if len(segs) > 1:
            total += sum(s.adj_time_s for s in segs[1:])
        return total


def segment_to_dict(s: Segment) -> dict:
    d = asdict(s)
    d["coords"] = [list(c) for c in s.coords]
    return d
