"""Build the routable road network in PostGIS.

Two sources:
  * import_osm(): real OpenStreetMap .pbf extract (parsed with pyosmium) clipped to the service radius.
  * generate_synthetic(): a jittered grid network around the configured city centre. This is the
    clearly-labelled DEMO FALLBACK used only until an OSM extract is installed.

Only the largest strongly-connected component is kept so that every node can reach every other node.
"""
from __future__ import annotations

import logging
import math
import random
import re
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.utils.geo import haversine_m

log = logging.getLogger("app.routing.import")

DEFAULT_SPEEDS_KPH = {
    "motorway": 80, "trunk": 70, "primary": 60, "secondary": 50, "tertiary": 40,
    "unclassified": 30, "residential": 30, "living_street": 15,
    "motorway_link": 50, "trunk_link": 40, "primary_link": 40, "secondary_link": 35, "tertiary_link": 30,
}
DRIVEABLE = set(DEFAULT_SPEEDS_KPH)


@dataclass
class RawNetwork:
    source: str
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lat, lon)
    # road_id -> dict(name, highway, speed, oneway, node_ids)
    roads: dict[str, dict] = field(default_factory=dict)


def parse_maxspeed(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph)?", value)
    if not m:
        return None
    speed = float(m.group(1))
    if m.group(2):
        speed *= 1.609344
    return speed if 5 <= speed <= 150 else None


def read_osm(pbf_path: str, center_lat: float, center_lon: float, radius_m: float) -> RawNetwork:
    import osmium  # imported lazily: only needed for OSM import

    net = RawNetwork(source="osm")

    class Handler(osmium.SimpleHandler):
        def way(self, w):  # noqa: N802 (osmium API)
            tags = w.tags
            hw = tags.get("highway")
            if hw not in DRIVEABLE:
                return
            if tags.get("access") in ("no", "private") or tags.get("motor_vehicle") == "no":
                return
            refs = []
            inside = False
            for n in w.nodes:
                if not n.location.valid():
                    continue
                lat, lon = n.location.lat, n.location.lon
                refs.append(n.ref)
                net.nodes[n.ref] = (lat, lon)
                if not inside and haversine_m(center_lat, center_lon, lat, lon) <= radius_m:
                    inside = True
            if len(refs) < 2 or not inside:
                return
            ow = tags.get("oneway", "")
            oneway = ow in ("yes", "1", "true") or hw == "motorway" or tags.get("junction") == "roundabout"
            if ow == "-1":
                refs.reverse()
                oneway = True
            speed = parse_maxspeed(tags.get("maxspeed")) or DEFAULT_SPEEDS_KPH[hw]
            net.roads[f"W{w.id}"] = {
                "name": tags.get("name") or tags.get("ref") or hw.replace("_", " ").title(),
                "highway": hw, "speed": float(speed), "oneway": oneway, "node_ids": refs,
            }

    Handler().apply_file(pbf_path, locations=True)
    log.info("osm parsed", extra={"event": "OSM_PARSED", "fields": {"roads": len(net.roads), "nodes": len(net.nodes)}})
    return net


def generate_synthetic(center_lat: float, center_lon: float, radius_m: float, seed: int = 7,
                       spacing_m: float = 450.0) -> RawNetwork:
    """Jittered Manhattan grid: arterials every 4th line (60 km/h), local streets 30-40 km/h."""
    rng = random.Random(seed)
    net = RawNetwork(source="synthetic")
    n = int(radius_m // spacing_m)
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))
    ids: dict[tuple[int, int], int] = {}
    nid = -1
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x = j * spacing_m + rng.uniform(-60, 60)
            y = i * spacing_m + rng.uniform(-60, 60)
            if math.hypot(x, y) > radius_m:
                continue
            ids[(i, j)] = nid
            net.nodes[nid] = (center_lat + y / m_per_deg_lat, center_lon + x / m_per_deg_lon)
            nid -= 1
    road_no = 100
    chunk = 3  # blocks per road record
    for axis in ("EW", "NS"):
        for line in range(-n, n + 1):
            arterial = line % 4 == 0
            seq = [(line, k) if axis == "EW" else (k, line) for k in range(-n, n + 1)]
            seq = [p for p in seq if p in ids]
            # split into contiguous runs then into chunks
            run: list[int] = []
            prev = None
            runs = []
            for p in seq:
                k = p[1] if axis == "EW" else p[0]
                if prev is not None and k != prev + 1:
                    runs.append(run)
                    run = []
                run.append(ids[p])
                prev = k
            runs.append(run)
            for run in runs:
                for start in range(0, len(run) - 1, chunk):
                    part = run[start:start + chunk + 1]
                    if len(part) < 2:
                        continue
                    road_no += 1
                    label = f"{'Ring' if arterial else ''} {'Avenue' if axis == 'NS' else 'Street'} {line + n + 1}".strip()
                    net.roads[f"R{road_no}"] = {
                        "name": ("Main " if arterial else "") + label,
                        "highway": "primary" if arterial else "residential",
                        "speed": 60.0 if arterial else rng.choice([30.0, 40.0]),
                        "oneway": False, "node_ids": part,
                    }
    return net


def _largest_scc(net: RawNetwork) -> tuple[set[int], list[tuple[str, int, int, float]]]:
    edges: list[tuple[str, int, int, float]] = []
    for rid, road in net.roads.items():
        refs = road["node_ids"]
        for a, b in zip(refs, refs[1:]):
            if a == b:
                continue
            la, oa = net.nodes[a]
            lb, ob = net.nodes[b]
            d = haversine_m(la, oa, lb, ob)
            edges.append((rid, a, b, d))
            if not road["oneway"]:
                edges.append((rid, b, a, d))
    node_list = sorted({e[1] for e in edges} | {e[2] for e in edges})
    index = {nid: i for i, nid in enumerate(node_list)}
    rows = np.array([index[e[1]] for e in edges])
    cols = np.array([index[e[2]] for e in edges])
    mat = csr_matrix((np.ones(len(edges)), (rows, cols)), shape=(len(node_list), len(node_list)))
    _, labels = connected_components(mat, directed=True, connection="strong")
    biggest = np.bincount(labels).argmax()
    keep = {node_list[i] for i in np.nonzero(labels == biggest)[0]}
    kept_edges = [e for e in edges if e[1] in keep and e[2] in keep]
    return keep, kept_edges


def write_network(engine: Engine, net: RawNetwork, center_lat: float, center_lon: float, radius_m: float,
                  city_name: str) -> dict:
    keep, edges = _largest_scc(net)
    road_edges: dict[str, list] = {}
    for e in edges:
        road_edges.setdefault(e[0], []).append(e)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE route_segments, routes, traffic_events, road_edges, road_conditions, road_nodes, service_areas CASCADE"))
        conn.execute(
            text("INSERT INTO service_areas(name, boundary) VALUES (:n, "
                 "ST_Buffer(ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography, :r)::geometry)"),
            {"n": city_name, "lat": center_lat, "lon": center_lon, "r": radius_m * 1.15},
        )
        conn.execute(
            text("INSERT INTO road_nodes(id, latitude, longitude, location) VALUES "
                 "(:id, :lat, :lon, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography)"),
            [{"id": nid, "lat": net.nodes[nid][0], "lon": net.nodes[nid][1]} for nid in keep],
        )
        road_rows = []
        for rid, elist in road_edges.items():
            road = net.roads[rid]
            pts = [nid for nid in road["node_ids"] if nid in keep]
            if len(pts) < 2:
                pts = [elist[0][1], elist[0][2]]
            wkt = "SRID=4326;LINESTRING(" + ",".join(f"{net.nodes[p][1]} {net.nodes[p][0]}" for p in pts) + ")"
            length = sum(e[3] for e in elist) / (1 if road["oneway"] else 2)
            road_rows.append({
                "rid": rid, "name": road["name"][:255], "hw": road["highway"], "spd": road["speed"],
                "len": length, "ow": road["oneway"], "geom": wkt,
            })
        conn.execute(
            text("INSERT INTO road_conditions(road_id, name, highway_type, speed_limit_kph, current_speed_kph,"
                 " length_m, oneway, geom) VALUES (:rid, :name, :hw, :spd, :spd, :len, :ow, ST_GeogFromText(:geom))"),
            road_rows,
        )
        conn.execute(
            text("INSERT INTO road_edges(road_id, from_node, to_node, length_m) VALUES (:r, :a, :b, :d)"),
            [{"r": e[0], "a": e[1], "b": e[2], "d": e[3]} for e in edges],
        )
    stats = {"source": net.source, "nodes": len(keep), "edges": len(edges), "roads": len(road_edges)}
    log.info("road network written", extra={"event": "ROAD_NETWORK_IMPORTED", "fields": stats})
    return stats
