"""Deterministic synthetic demo data.

    python -m app.seed [--reset] [--seed 42] [--synthetic-network]

* road network: OpenStreetMap import of OSM_PBF_PATH if the file exists, otherwise the synthetic grid
* users: admin / dispatcher / viewer (development passwords - change them)
* 20 ambulances, 5 hospitals placed on road nodes spread over the service area
* initial congestion for ~15% of roads (all roads have a road_conditions record)
* 50 HISTORICAL_SEED incidents over the past 7 days. Their response times are computed from graph-routed
  ETAs of the nearest base with a sampled historical congestion factor - synthetic, clearly labelled.
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import uuid
from datetime import timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import select, text

from app.config import get_settings
from app.database import get_engine, run_migrations, session_scope
from app.dispatch.scoring import HospitalInput, hospital_requirements, score_hospitals
from app.dispatch.severity import combine_severity, required_capability, severity_score
from app.ml.dataset import CONSCIOUSNESS, GRADE, generate
from app.ml.predict import ModelUnavailable, SeverityModel
from app.models import Ambulance, EmergencyIncident, Hospital, RoadCondition, TrafficEvent, User
from app.models.entities import point_wkt
from app.routing.engine import RoutingEngine
from app.routing.graph import RoadGraph
from app.routing.network_import import generate_synthetic, read_osm, write_network
from app.routing.traffic import LEVEL_DENSITY, adjusted_speed_mps
from app.services.auth_service import hash_password
from app.utils.geo import haversine_m
from app.utils.logging import configure_logging
from app.utils.timeutil import utcnow

log = logging.getLogger("app.seed")

HOSPITAL_TEMPLATES = [
    # name, capacity, icu, trauma, cardiac, stroke
    ("City General Hospital (synthetic)", 40, 8, True, True, True),
    ("Northside Medical Centre (synthetic)", 25, 0, False, True, False),
    ("St. Clara Trauma Institute (synthetic)", 30, 6, True, False, False),
    ("Lakeside Community Hospital (synthetic)", 15, 0, False, False, False),
    ("Heart & Stroke Specialty Clinic (synthetic)", 20, 4, False, True, True),
    ("Eastgate Emergency Hospital (synthetic)", 25, 2, True, False, True),
    ("Riverside Clinic (synthetic)", 12, 0, False, False, False),
]
EQUIPMENT_MIX = ["BASIC", "ADVANCED", "BASIC", "ICU", "ADVANCED"]
OPERATIONAL_TABLES = ("system_events, iot_messages, model_predictions, ambulance_locations, route_segments, routes, "
                      "dispatches, traffic_events, emergency_incidents, ambulances, hospitals")


def ensure_network(force_synthetic: bool = False) -> dict:
    s = get_settings()
    engine = get_engine()
    with engine.connect() as conn:
        if conn.execute(text("SELECT count(*) FROM road_edges")).scalar():
            return {"status": "existing"}
    pbf = Path(s.osm_pbf_path)
    if not pbf.is_absolute():
        pbf = (Path(__file__).resolve().parent.parent / pbf).resolve()
    if pbf.exists() and not force_synthetic:
        net = read_osm(str(pbf), s.city_lat, s.city_lon, s.city_radius_m)
    else:
        log.warning("OSM extract not found - generating SYNTHETIC grid network (demo fallback)",
                    extra={"event": "SYNTHETIC_NETWORK", "fields": {"expected_pbf": str(pbf)}})
        net = generate_synthetic(s.city_lat, s.city_lon, s.city_radius_m)
    return write_network(engine, net, s.city_lat, s.city_lon, s.city_radius_m, s.city_name)


def spread_nodes(g: RoadGraph, k: int, rng: random.Random, center: tuple[float, float], radius_m: float,
                 exclude: list[int] | None = None) -> list[int]:
    """Farthest-point sampling over road nodes inside 85% of the service radius."""
    lat0, lon0 = center
    dist = np.array([haversine_m(lat0, lon0, a, b) for a, b in zip(g.lat, g.lon)])
    pool = np.nonzero(dist <= radius_m * 0.85)[0]
    if len(pool) < k:
        pool = np.arange(len(g.lat))
    chosen = list(exclude or [])
    picks: list[int] = []
    if not chosen:
        first = int(pool[rng.randrange(len(pool))])
        picks.append(first)
        chosen.append(first)
    kx = 111_320 * math.cos(math.radians(lat0))
    xy = np.column_stack([g.lon * kx, g.lat * 110_540])
    while len(picks) < k:
        d = np.min(np.linalg.norm(xy[pool][:, None, :] - xy[chosen][None, :, :], axis=2), axis=1)
        # mix of farthest point and randomness for natural-looking placement
        cand = pool[np.argsort(-d)[:max(3, len(pool) // 50)]]
        nxt = int(cand[rng.randrange(len(cand))])
        picks.append(nxt)
        chosen.append(nxt)
    return picks


def seed(reset: bool = False, seed_value: int = 42, n_ambulances: int = 20, n_hospitals: int = 5,
         n_historical: int = 50, force_synthetic: bool = False) -> dict:
    s = get_settings()
    rng = random.Random(seed_value)
    run_migrations()
    engine = get_engine()
    if reset:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {OPERATIONAL_TABLES} CASCADE"))
            conn.execute(text("UPDATE road_conditions SET congestion_level='FREE', blocked=false, incident_multiplier=1, "
                              "current_speed_kph=speed_limit_kph, vehicle_density=0.01"))
    net = ensure_network(force_synthetic)
    g = RoadGraph.load(engine)
    router = RoutingEngine(g)
    center = (s.city_lat, s.city_lon)
    out = {"network": net, "graph": g.stats()}
    with session_scope() as db:
        for username, role, pw in (("admin", "ADMIN", "admin123"), ("dispatcher", "DISPATCHER", "dispatch123"),
                                   ("viewer", "VIEWER", "viewer123")):
            if not db.scalar(select(User).where(User.username == username)):
                db.add(User(username=username, full_name=username.title(), role=role, password_hash=hash_password(pw)))
        if db.scalar(text("SELECT count(*) FROM hospitals")):
            out["status"] = "fleet already seeded (use --reset to recreate)"
            return out
        h_nodes = spread_nodes(g, n_hospitals, rng, center, s.city_radius_m)
        hospitals = []
        for i, node in enumerate(h_nodes):
            name, cap, icu, trauma, cardiac, stroke = HOSPITAL_TEMPLATES[i % len(HOSPITAL_TEMPLATES)]
            lat, lon = float(g.lat[node]), float(g.lon[node])
            h = Hospital(id=f"HSP-{i + 1:03d}", name=name, latitude=lat, longitude=lon, location=point_wkt(lat, lon),
                         emergency_capacity=cap, icu_available=icu, trauma_available=trauma, cardiac_available=cardiac,
                         stroke_available=stroke, current_load=rng.randint(0, cap // 2), status="ACTIVE")
            db.add(h)
            hospitals.append(h)
        a_nodes = spread_nodes(g, n_ambulances, rng, center, s.city_radius_m, exclude=list(h_nodes))
        ambulances = []
        for i, node in enumerate(a_nodes):
            lat, lon = float(g.lat[node]), float(g.lon[node])
            a = Ambulance(id=f"AMB-{i + 1:03d}", call_sign=f"MEDIC {i + 1}", latitude=lat, longitude=lon,
                          location=point_wkt(lat, lon), base_latitude=lat, base_longitude=lon, status="AVAILABLE",
                          capacity=1 if i % 5 else 2, equipment_level=EQUIPMENT_MIX[i % len(EQUIPMENT_MIX)],
                          driver_status="ON_DUTY", fuel_level=round(rng.uniform(55, 100), 1), current_speed=0,
                          missions_today=rng.randint(0, 3), last_updated=utcnow())
            db.add(a)
            ambulances.append(a)
        db.flush()

        # initial traffic state (~15% of roads), recorded as SEED traffic events
        roads = db.scalars(select(RoadCondition).order_by(RoadCondition.road_id)).all()
        changed = 0
        for r in roads:
            if rng.random() < 0.15:
                lvl = rng.choice(["LIGHT", "LIGHT", "MODERATE", "MODERATE", "HEAVY"])
                r.congestion_level = lvl
                r.current_speed_kph = adjusted_speed_mps(r.speed_limit_kph, lvl) * 3.6
                r.vehicle_density = LEVEL_DENSITY[lvl]
                db.add(TrafficEvent(road_id=r.road_id, event_type="CONGESTION", old_level="FREE", new_level=lvl,
                                    blocked=False, incident_multiplier=1.0, source="SEED", active=False))
                g.set_road_state(r.road_id, lvl, 1.0, False)
                changed += 1
        out["road_conditions"] = {"roads": len(roads), "initially_congested": changed}

        # historical incidents
        model = SeverityModel()
        model.load()
        cases = generate(n=n_historical, seed=seed_value + 1).to_dict("records")
        inc_nodes = [int(rng.randrange(len(g.lat))) for _ in range(n_historical * 3)]
        inc_nodes = [n for n in inc_nodes if haversine_m(center[0], center[1], g.lat[n], g.lon[n]) <= s.city_radius_m * 0.9]
        now = utcnow()
        for k in range(n_historical):
            node = inc_nodes[k % len(inc_nodes)]
            c = cases[k]
            lat, lon = float(g.lat[node]) + rng.uniform(-1e-4, 1e-4), float(g.lon[node]) + rng.uniform(-1e-4, 1e-4)
            case = {"patient_age": c["age"], "heart_rate": c["heart_rate"], "respiratory_rate": c["respiratory_rate"],
                    "oxygen_saturation": c["oxygen_saturation"], "consciousness": CONSCIOUSNESS[c["consciousness"]],
                    "bleeding": GRADE[c["bleeding"]], "injury_severity": GRADE[c["injury_severity"]],
                    "accident_type": c["accident_type"], "breathing_difficulty": bool(c["breathing_difficulty"]),
                    "chest_pain": bool(c["chest_pain"]), "emergency_type": c["emergency_type"]}
            rule = severity_score(case)
            try:
                pred = model.predict(case)
                ml, conf, status = pred.severity, pred.confidence, "OK"
            except ModelUnavailable:
                ml, conf, status = None, None, "UNAVAILABLE"
            final, basis = combine_severity(ml, rule.level)
            nearest = min(ambulances, key=lambda a: haversine_m(a.base_latitude, a.base_longitude, lat, lon))
            rr = router.route((nearest.base_latitude, nearest.base_longitude), (lat, lon), compute_shortest=False)
            hist_factor = rng.uniform(1.0, 1.6)            # sampled historical congestion slowdown
            dispatch_delay = rng.uniform(15, 90)           # call-handling + decision time
            response = dispatch_delay + rr.base_duration_s * hist_factor
            hins = []
            for h in hospitals:
                hr = router.route((lat, lon), (h.latitude, h.longitude), compute_shortest=False)
                hins.append(HospitalInput(h.id, h.name, hr.base_duration_s, 0.0, hr.distance_m, h.current_load,
                                          h.emergency_capacity, h.icu_available, h.trauma_available,
                                          h.cardiac_available, h.stroke_available))
            hosp = score_hospitals(hins, hospital_requirements(final, case["emergency_type"]))[0]
            created = now - timedelta(days=rng.uniform(0.2, 7), minutes=rng.uniform(0, 60))
            k_scale = s.sim_time_scale
            db.add(EmergencyIncident(
                id=uuid.uuid4(), reference=f"HIS-{k + 1:04d}", created_at=created, latitude=lat, longitude=lon,
                location=point_wkt(lat, lon), emergency_type=case["emergency_type"], patient_age=case["patient_age"],
                heart_rate=case["heart_rate"], respiratory_rate=case["respiratory_rate"],
                oxygen_saturation=case["oxygen_saturation"], consciousness=case["consciousness"],
                bleeding=case["bleeding"], injury_severity=case["injury_severity"], accident_type=case["accident_type"],
                breathing_difficulty=case["breathing_difficulty"], chest_pain=case["chest_pain"],
                rule_score=rule.score, rule_severity=rule.level, rule_components=rule.components,
                predicted_severity=ml, ml_confidence=conf, ml_status=status, severity=final,
                severity_reasons=rule.reasons + [f"final severity basis: {basis}"],
                required_capability=required_capability(final, case["emergency_type"]),
                assigned_ambulance=nearest.id, destination_hospital=hosp["hospital_id"], status="COMPLETED",
                dispatched_at=created + timedelta(seconds=dispatch_delay / k_scale),
                arrived_at=created + timedelta(seconds=response / k_scale),
                completed_at=created + timedelta(seconds=(response + 1500) / k_scale),
                source="HISTORICAL_SEED", historical_response_s=round(response, 1),
                historical_dispatch_s=round(dispatch_delay, 1), notes="Synthetic historical record (seed data)"))
        out.update({"hospitals": len(hospitals), "ambulances": len(ambulances), "historical_incidents": n_historical,
                    "status": "seeded"})
    return out


if __name__ == "__main__":
    configure_logging()
    ap = argparse.ArgumentParser(description="Seed synthetic demo data")
    ap.add_argument("--reset", action="store_true", help="wipe operational tables first")
    ap.add_argument("--reset-network", action="store_true", help="also re-import the road network")
    ap.add_argument("--synthetic-network", action="store_true", help="force the synthetic grid network")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ambulances", type=int, default=20)
    ap.add_argument("--hospitals", type=int, default=5)
    ap.add_argument("--historical", type=int, default=50)
    a = ap.parse_args()
    if a.reset_network:
        run_migrations()
        with get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE {OPERATIONAL_TABLES}, road_edges, road_conditions, road_nodes, service_areas CASCADE"))
    result = seed(a.reset or a.reset_network, a.seed, a.ambulances, a.hospitals, a.historical, a.synthetic_network)
    import json
    print(json.dumps(result, indent=2, default=str))
