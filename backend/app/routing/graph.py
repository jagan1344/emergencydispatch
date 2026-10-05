"""In-memory directed road graph with live traffic weights.

The graph is loaded once from PostGIS (road_nodes / road_edges / road_conditions). Traffic updates only
change per-road arrays, and edge weights are recomputed vectorised with numpy. Shortest paths use
scipy's C implementation of Dijkstra, so a 100k-node city graph is routed in tens of milliseconds.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.routing.traffic import CONGESTION_FACTORS

MIN_WEIGHT = 1e-3


class GraphNotLoaded(RuntimeError):
    pass


@dataclass
class RoadState:
    level: str
    incident_multiplier: float
    blocked: bool


class RoadGraph:
    def __init__(self, node_ids, lat, lon, e_from, e_to, e_len, e_road, road_ids, road_names, road_limit_kph,
                 road_highway, source: str):
        self.lock = threading.RLock()
        self.source = source
        self.node_ids = np.asarray(node_ids, dtype=np.int64)
        self.lat = np.asarray(lat, dtype=float)
        self.lon = np.asarray(lon, dtype=float)
        self.idx_of = {int(n): i for i, n in enumerate(self.node_ids)}
        self.e_from = np.asarray(e_from, dtype=np.int64)
        self.e_to = np.asarray(e_to, dtype=np.int64)
        self.e_len = np.asarray(e_len, dtype=float)
        self.e_road = np.asarray(e_road, dtype=np.int64)
        self.road_ids = list(road_ids)
        self.road_idx = {r: i for i, r in enumerate(self.road_ids)}
        self.road_names = list(road_names)
        self.road_highway = list(road_highway)
        self.road_limit_kph = np.asarray(road_limit_kph, dtype=float)
        n_roads = len(self.road_ids)
        self.road_factor = np.ones(n_roads)          # congestion factor
        self.road_multiplier = np.ones(n_roads)      # incident multiplier
        self.road_blocked = np.zeros(n_roads, dtype=bool)
        self.road_level = ["FREE"] * n_roads
        self.version = 0
        self._cache: dict[tuple, csr_matrix] = {}
        # (u_idx, v_idx) -> edge index (shortest parallel edge wins)
        self.pair_edge: dict[tuple[int, int], int] = {}
        order = np.argsort(self.e_len)
        for ei in order:
            key = (int(self.e_from[ei]), int(self.e_to[ei]))
            self.pair_edge.setdefault(key, int(ei))
        lat0 = float(np.mean(self.lat)) if len(self.lat) else 0.0
        self._kx = 111_320.0 * math.cos(math.radians(lat0))
        self._ky = 110_540.0
        self._tree = cKDTree(np.column_stack([self.lon * self._kx, self.lat * self._ky]))

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, engine: Engine) -> "RoadGraph":
        with engine.connect() as conn:
            nodes = conn.execute(text("SELECT id, latitude, longitude FROM road_nodes ORDER BY id")).all()
            roads = conn.execute(text(
                "SELECT road_id, name, highway_type, speed_limit_kph, congestion_level, incident_multiplier, blocked "
                "FROM road_conditions ORDER BY road_id")).all()
            edges = conn.execute(text("SELECT road_id, from_node, to_node, length_m FROM road_edges ORDER BY id")).all()
        if not nodes or not edges:
            raise GraphNotLoaded("road network is empty - run the seed / OSM import first")
        node_ids = [r[0] for r in nodes]
        idx = {n: i for i, n in enumerate(node_ids)}
        road_ids = [r[0] for r in roads]
        ridx = {r: i for i, r in enumerate(road_ids)}
        source = "synthetic" if node_ids[0] < 0 else "osm"
        g = cls(
            node_ids, [r[1] for r in nodes], [r[2] for r in nodes],
            [idx[e[1]] for e in edges], [idx[e[2]] for e in edges], [e[3] for e in edges], [ridx[e[0]] for e in edges],
            road_ids, [r[1] for r in roads], [r[3] for r in roads], [r[2] for r in roads], source,
        )
        for r in roads:
            g.set_road_state(r[0], r[4], r[5], r[6])
        return g

    # ------------------------------------------------------------------ traffic state
    def set_road_state(self, road_id: str, level: str, incident_multiplier: float, blocked: bool) -> None:
        i = self.road_idx.get(road_id)
        if i is None:
            return
        with self.lock:
            blocked = blocked or level == "BLOCKED"
            self.road_level[i] = level
            self.road_factor[i] = CONGESTION_FACTORS.get(level, 1.0)
            self.road_multiplier[i] = incident_multiplier
            self.road_blocked[i] = blocked
            self.version += 1
            self._cache.clear()

    def road_state(self, road_id: str) -> RoadState | None:
        i = self.road_idx.get(road_id)
        if i is None:
            return None
        return RoadState(self.road_level[i], float(self.road_multiplier[i]), bool(self.road_blocked[i]))

    def road_speed_mps(self, road_i: int, adjusted: bool = True) -> float:
        base = self.road_limit_kph[road_i] / 3.6
        if not adjusted:
            return float(base)
        if self.road_blocked[road_i]:
            return 0.0
        return float(base * self.road_factor[road_i] * self.road_multiplier[road_i])

    def edge_weights(self, kind: str) -> np.ndarray:
        """kind: 'time' (traffic-adjusted s), 'free' (free-flow s), 'length' (m). inf = unusable."""
        if kind == "length":
            return self.e_len.copy()
        speed = self.road_limit_kph[self.e_road] / 3.6
        if kind == "free":
            return self.e_len / speed
        eff = speed * self.road_factor[self.e_road] * self.road_multiplier[self.e_road]
        eff = np.where(self.road_blocked[self.e_road], 0.0, eff)
        with np.errstate(divide="ignore"):
            return np.where(eff > 0, self.e_len / np.maximum(eff, 1e-9), np.inf)

    def _matrix(self, kind: str, reverse: bool = False) -> csr_matrix:
        key = (kind, reverse, self.version)
        with self.lock:
            m = self._cache.get(key)
            if m is not None:
                return m
            w = self.edge_weights(kind)
            ok = np.isfinite(w)
            src, dst, w = self.e_from[ok], self.e_to[ok], np.maximum(w[ok], MIN_WEIGHT)
            if reverse:
                src, dst = dst, src
            # csr_matrix would SUM duplicate (u,v) entries; keep only the cheapest parallel edge.
            order = np.lexsort((w, dst, src))
            src, dst, w = src[order], dst[order], w[order]
            first = np.ones(len(src), dtype=bool)
            first[1:] = (src[1:] != src[:-1]) | (dst[1:] != dst[:-1])
            n = len(self.node_ids)
            m = csr_matrix((w[first], (src[first], dst[first])), shape=(n, n))
            self._cache[key] = m
            return m

    # ------------------------------------------------------------------ queries
    def nearest_node(self, lat: float, lon: float) -> tuple[int, float]:
        d, i = self._tree.query([lon * self._kx, lat * self._ky])
        return int(i), float(d)

    def shortest_path(self, src: int, dst: int, kind: str = "time") -> tuple[list[int], float]:
        """Returns (node index path, cost). Empty path + inf when unreachable."""
        if src == dst:
            return [src], 0.0
        dist, pred = dijkstra(self._matrix(kind), directed=True, indices=src, return_predecessors=True)
        if not np.isfinite(dist[dst]):
            return [], math.inf
        path = [dst]
        while path[-1] != src:
            p = pred[path[-1]]
            if p < 0:
                return [], math.inf
            path.append(int(p))
        path.reverse()
        return path, float(dist[dst])

    def edge_between(self, u: int, v: int, kind: str = "time") -> int | None:
        return self.pair_edge.get((u, v))

    def path_edges(self, path: list[int]) -> list[int]:
        out = []
        for u, v in zip(path, path[1:]):
            e = self.pair_edge.get((u, v))
            if e is None:
                raise ValueError(f"no edge between graph nodes {u}->{v}")
            out.append(e)
        return out

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for lvl in self.road_level:
            counts[lvl] = counts.get(lvl, 0) + 1
        return {"source": self.source, "nodes": len(self.node_ids), "edges": len(self.e_from),
                "roads": len(self.road_ids), "blocked_roads": int(self.road_blocked.sum()),
                "levels": counts, "version": self.version}
