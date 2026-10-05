"""Starts the ambulance simulator and (optionally) the traffic simulator in one process.

    python simulator/run_simulator.py                 # ambulances + dynamic traffic
    python simulator/run_simulator.py --no-traffic    # ambulances only (traffic changed by dispatcher/scenario)
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ambulance_simulator import AmbulanceSimulator  # noqa: E402
from common import load_env  # noqa: E402
from traffic_simulator import TrafficSimulator, parser  # noqa: E402

if __name__ == "__main__":
    load_env()
    ap = parser()
    ap.add_argument("--no-traffic", action="store_true")
    ap.add_argument("--tick", type=float, default=1.0)
    a = ap.parse_args()
    host, port = os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", 1883))
    stop = threading.Event()
    threads = [threading.Thread(target=AmbulanceSimulator(host, port, a.tick).run, args=(stop,), daemon=True)]
    if not a.no_traffic:
        threads.append(threading.Thread(target=TrafficSimulator(host, port, a.seed, a.min_interval, a.max_interval,
                                                                a.changes, a.target_routes, a.accident_rate).run,
                                        args=(stop,), daemon=True))
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
