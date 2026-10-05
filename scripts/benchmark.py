"""Micro-benchmarks on the configured road network (run from the project root, backend DB seeded):

    python scripts/benchmark.py

Measures wall-clock latency of: graph routing, full RoutingEngine.route (graph + OSRM if configured),
ML prediction, the scoring function for 100 candidates, and a telemetry flush of 100 ambulance fixes.
Numbers depend on the machine; report them together with the hardware they were measured on.
"""
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.config import get_settings  # noqa: E402
from app.database import get_engine  # noqa: E402
from app.dispatch.scoring import CandidateInput, score_candidates  # noqa: E402
from app.ml.predict import SeverityModel  # noqa: E402
from app.routing.engine import RoutingEngine  # noqa: E402
from app.routing.graph import RoadGraph  # noqa: E402
from app.routing.osrm_client import OsrmClient  # noqa: E402
from app.services.telemetry_service import TelemetryBuffer  # noqa: E402


def timed(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return {"n": n, "median_ms": round(statistics.median(out), 2), "p95_ms": round(sorted(out)[int(n * 0.95) - 1], 2)}


def main():
    rng = random.Random(1)
    s = get_settings()
    g = RoadGraph.load(get_engine())
    pairs = [((float(g.lat[a]), float(g.lon[a])), (float(g.lat[b]), float(g.lon[b])))
             for a, b in ((rng.randrange(len(g.lat)), rng.randrange(len(g.lat))) for _ in range(200))]
    it = iter(pairs * 10)
    res = {"graph": g.stats()}
    res["dijkstra_graph_only"] = timed(lambda: RoutingEngine(g).route(*next(it), compute_shortest=False), 100)
    if s.osrm_url:
        eng = RoutingEngine(g, OsrmClient(s.osrm_url))
        res["route_graph_plus_osrm"] = timed(lambda: eng.route(*next(it), compute_shortest=False), 100)
    m = SeverityModel()
    m.load()
    case = {"patient_age": 60, "heart_rate": 120, "respiratory_rate": 25, "oxygen_saturation": 90, "consciousness": "VERBAL",
            "bleeding": "MINOR", "injury_severity": "MODERATE", "accident_type": "ROAD", "breathing_difficulty": True,
            "chest_pain": False, "emergency_type": "accident"}
    res["ml_predict"] = timed(lambda: m.predict(case), 200)
    cands = [CandidateInput(f"A{i}", rng.choice(["BASIC", "ADVANCED", "ICU"]), rng.uniform(60, 900), rng.uniform(300, 9000),
                            rng.uniform(0, 200), rng.randint(0, 6), rng.uniform(10, 100)) for i in range(100)]
    res["score_100_candidates"] = timed(lambda: score_candidates(cands, "ICU"), 200)
    ambs = [r[0] for r in get_engine().connect().exec_driver_sql("SELECT id FROM ambulances").all()]
    buf = TelemetryBuffer()

    def flush100():
        for k in range(100):
            buf.handle_location(ambs[k % len(ambs)], {"latitude": float(g.lat[k]), "longitude": float(g.lon[k]), "speed": 30})
        buf.flush(get_engine())
    res["telemetry_flush_100_fixes"] = timed(flush100, 20)
    import json
    import platform
    res["machine"] = {"python": platform.python_version(), "cpu_count": os.cpu_count(), "platform": platform.platform()}
    print(json.dumps(res, indent=2))
    print("NOTE: the telemetry benchmark wrote 2000 rows to ambulance_locations and moved ambulances; re-seed afterwards.")


if __name__ == "__main__":
    main()
