"""Dynamic traffic + hospital sensor simulator (seeded, reproducible).

Every 5-10 s (wall clock) it changes the state of a few roads:
  * Markov drift of congestion levels (FREE ↔ LIGHT ↔ MODERATE ↔ HEAVY ↔ SEVERE)
  * random ACCIDENT events (SEVERE + 0.5 incident multiplier) that may escalate to BLOCKED
  * clearing of earlier accidents / closures
  * vehicle density changes
Optionally a fraction of events targets roads on active ambulance routes (--target-routes 0.3) so that
re-routing is exercised regularly. Publishes traffic/{road_id}/status (retained), traffic/{road_id}/speed,
traffic/events and, for hospitals, discharge events on hospital/{id}/capacity.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from datetime import datetime, timezone

from common import CONGESTION_FACTORS, LEVELS, backend_client, jmsg, load_env, mqtt_client, setup_logging

log = setup_logging("traffic-sim")
DENSITY = {"FREE": 0.01, "LIGHT": 0.03, "MODERATE": 0.05, "HEAVY": 0.08, "SEVERE": 0.12, "BLOCKED": 0.15}


class TrafficSimulator:
    def __init__(self, host: str, port: int, seed: int = 42, min_interval: float = 5, max_interval: float = 10,
                 changes: int = 4, target_routes: float = 0.0, accident_rate: float = 0.08):
        self.rng = random.Random(seed)
        self.host, self.port = host, port
        self.min_interval, self.max_interval = min_interval, max_interval
        self.changes, self.target_routes, self.accident_rate = changes, target_routes, accident_rate
        self.roads: dict[str, dict] = {}
        self.hospitals: list[str] = []
        self.accidents: dict[str, float] = {}     # road_id -> created time
        self.client = mqtt_client("traffic-sim")
        self.api = None
        self.events = 0

    def load(self) -> None:
        self.api = backend_client()
        roads = self.api.get("/api/traffic/roads").json()
        self.roads = {r["road_id"]: {"speed_limit_kph": r["speed_limit_kph"], "level": r["congestion_level"],
                                     "blocked": r["blocked"], "mult": r["incident_multiplier"],
                                     "major": r["highway_type"] in ("primary", "secondary", "tertiary", "trunk", "motorway")}
                      for r in roads}
        self.hospitals = [h["id"] for h in self.api.get("/api/hospitals").json()]
        log.info(jmsg(event="TRAFFIC_SIM_LOADED", roads=len(self.roads), hospitals=len(self.hospitals)))

    def route_roads(self) -> list[str]:
        try:
            routes = self.api.get("/api/routes", params={"active": True}).json()
            out = []
            for r in routes:
                full = self.api.get(f"/api/routes/{r['id']}").json()
                out += [s["road_id"] for s in full.get("segments", [])[3:-3] if s["road_id"]]
            return out
        except Exception:
            return []

    def publish_state(self, road_id: str, event_type: str) -> None:
        st = self.roads[road_id]
        factor = 0.0 if st["blocked"] else CONGESTION_FACTORS[st["level"]] * st["mult"]
        speed = st["speed_limit_kph"] * factor
        now = datetime.now(timezone.utc).isoformat()
        payload = {"road_id": road_id, "congestion_level": st["level"], "blocked": st["blocked"],
                   "incident_multiplier": st["mult"], "speed_limit_kph": st["speed_limit_kph"],
                   "current_speed_kph": round(speed, 2), "vehicle_density": DENSITY[st["level"]],
                   "event_type": event_type, "source": "SIMULATOR", "ts": now}
        self.client.publish(f"traffic/{road_id}/status", json.dumps(payload), qos=1, retain=True)
        self.client.publish(f"traffic/{road_id}/speed", json.dumps({"road_id": road_id, "avg_speed_kph": round(speed, 2),
                                                                    "ts": now, "source": "SIMULATOR"}))
        self.client.publish("traffic/events", json.dumps(payload))
        self.events += 1
        log.info(jmsg(event="TRAFFIC_EVENT", road_id=road_id, type=event_type, level=st["level"], blocked=st["blocked"]))

    def change(self, road_id: str) -> None:
        st = self.roads[road_id]
        now = time.time()
        if road_id in self.accidents:
            age = now - self.accidents[road_id]
            if not st["blocked"] and self.rng.random() < 0.3 and age < 40:
                st.update(level="BLOCKED", blocked=True)
                return self.publish_state(road_id, "BLOCK")
            if age > 25 and self.rng.random() < 0.6:
                del self.accidents[road_id]
                st.update(level="MODERATE", blocked=False, mult=1.0)
                return self.publish_state(road_id, "CLEAR")
            return None
        if self.rng.random() < self.accident_rate:
            self.accidents[road_id] = now
            st.update(level="SEVERE", blocked=False, mult=0.5)
            return self.publish_state(road_id, "ACCIDENT")
        idx = LEVELS.index(st["level"]) if st["level"] != "BLOCKED" else 4
        step = self.rng.choice([-1, -1, 1, 1, 2]) if idx < 2 else self.rng.choice([-2, -1, -1, 1])
        st.update(level=LEVELS[max(0, min(4, idx + step))], blocked=False)
        self.publish_state(road_id, "CONGESTION")

    def tick(self) -> None:
        ids = list(self.roads)
        weights = [3.0 if self.roads[r]["major"] else 1.0 for r in ids]
        targets = self.rng.choices(ids, weights=weights, k=self.changes)
        if self.target_routes > 0 and self.rng.random() < self.target_routes:
            on_route = [r for r in self.route_roads() if r in self.roads]
            if on_route:
                targets[0] = self.rng.choice(on_route)
        for rid in list(self.accidents):           # give existing incidents a chance to evolve/clear
            if rid not in targets:
                targets.append(rid)
        for rid in targets:
            self.change(rid)
        if self.hospitals and self.rng.random() < 0.25:
            hid = self.rng.choice(self.hospitals)
            self.client.publish(f"hospital/{hid}/capacity", json.dumps(
                {"hospital_id": hid, "event": "DISCHARGE", "count": 1, "source": "SIMULATOR",
                 "ts": datetime.now(timezone.utc).isoformat()}), qos=1)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        for attempt in range(30):
            try:
                self.load()
                break
            except Exception as exc:
                log.warning(jmsg(event="BACKEND_NOT_READY", error=str(exc)[:120], attempt=attempt))
                if stop.wait(2):
                    return
        else:
            raise SystemExit("backend not reachable")
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        next_tick = time.time() + self.rng.uniform(self.min_interval, self.max_interval)
        try:
            while not stop.is_set():
                stop.wait(1.0)
                self.client.publish("simulator/heartbeat", json.dumps({"component": "traffic", "events": self.events}))
                if time.time() >= next_tick:
                    self.tick()
                    next_tick = time.time() + self.rng.uniform(self.min_interval, self.max_interval)
        finally:
            self.client.loop_stop()
            self.client.disconnect()


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Traffic + hospital sensor simulator")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-interval", type=float, default=5)
    ap.add_argument("--max-interval", type=float, default=10)
    ap.add_argument("--changes", type=int, default=4, help="roads changed per tick")
    ap.add_argument("--target-routes", type=float, default=0.0, help="probability that a tick hits an active route")
    ap.add_argument("--accident-rate", type=float, default=0.08)
    return ap


if __name__ == "__main__":
    load_env()
    a = parser().parse_args()
    TrafficSimulator(os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", 1883)), a.seed,
                     a.min_interval, a.max_interval, a.changes, a.target_routes, a.accident_rate).run()
