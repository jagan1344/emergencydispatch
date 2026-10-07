"""Ambulance GPS / telemetry simulator.

Receives FOLLOW_ROUTE commands (MQTT ambulance/{id}/command), drives each ambulance along the route
geometry at the CURRENT traffic-adjusted speed of the road it is on (traffic state learnt from the
retained traffic/{road_id}/status topics) and publishes:
  ambulance/{id}/location   every tick   {ambulance_id, latitude, longitude, speed, heading, timestamp, route_id, progress_m}
  ambulance/{id}/status     on events    ROUTE_STARTED / ARRIVED
  ambulance/{id}/telemetry  every 5 s    {fuel_level, odometer_km, engine_temp_c}
  simulator/heartbeat       every 2 s
  simulator/hello           on every (re)connect
Simulated time runs time_scale × faster than wall-clock (time_scale comes from the backend command).

Restart recovery: a route command the broker replays as RETAINED (this process just started and has no state
for the unit) is held, not driven. The backend answers simulator/hello with the same route plus a `resume`
block (last persisted GPS fix + progress along the route); the unit then continues from that point - no
teleport to the route start, no new route. If no answer arrives within RESUME_WAIT_S (e.g. an older backend),
the held command is started as before and RESUME_UNAVAILABLE is logged.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common import CONGESTION_FACTORS, bearing_deg, haversine_m, jmsg, load_env, mqtt_client, setup_logging

log = setup_logging("ambulance-sim")
FUEL_PCT_PER_KM = 0.6      # simulated consumption
RESUME_WAIT_S = float(os.environ.get("SIM_RESUME_WAIT_S", 8.0))
RESUME_MAX_OFFSET_M = 75.0  # resume point and last GPS fix must agree
SEGMENT_ENTRY_EPS = 1e-6


@dataclass
class Seg:
    road_id: str | None
    length_m: float
    speed_kph: float          # planned speed (fallback when no traffic info for road)
    base_speed_kph: float
    coords: list[tuple[float, float]]


@dataclass
class Mission:
    route_id: str
    leg: str
    time_scale: float
    segs: list[Seg]
    seg_idx: int = 0
    pos_in_seg: float = 0.0
    progress_m: float = 0.0
    started: bool = False
    arrived: bool = False


@dataclass
class Unit:
    ambulance_id: str
    mission: Mission | None = None
    lat: float | None = None
    lon: float | None = None
    speed_kph: float = 0.0
    heading: float = 0.0
    fuel: float = 85.0
    odometer_m: float = 0.0
    last_telemetry: float = field(default_factory=time.time)
    pending: tuple[dict, float] | None = None     # retained command held until the backend sends the resume point


class AmbulanceSimulator:
    def __init__(self, host: str, port: int, tick_s: float = 1.0):
        self.host, self.port, self.tick_s = host, port, tick_s
        self.units: dict[str, Unit] = {}
        self.roads: dict[str, dict] = {}           # road_id -> latest traffic status
        self.lock = threading.Lock()
        self.client = mqtt_client("ambulance-sim")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.running = False
        self.published = 0

    # -------------------------------------------------------------- MQTT
    def _on_connect(self, client, userdata, flags, rc, props=None):
        log.info(jmsg(event="MQTT_CONNECTED", host=self.host))
        client.subscribe([("ambulance/+/command", 1), ("traffic/+/status", 1)])
        client.publish("simulator/hello", json.dumps({"component": "ambulance", "known_units": len(self.units),
                                                      "ts": datetime.now(timezone.utc).isoformat()}), qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode()) if msg.payload else {}
        except ValueError:
            return
        parts = msg.topic.split("/")
        if parts[0] == "traffic":
            with self.lock:
                self.roads[parts[1]] = payload
        elif parts[0] == "ambulance" and parts[2] == "command":
            self._command(parts[1], payload, retained=bool(msg.retain))

    def _command(self, amb_id: str, p: dict, retained: bool = False) -> None:
        with self.lock:
            self._apply_command(amb_id, p, retained)

    def _apply_command(self, amb_id: str, p: dict, retained: bool = False) -> None:
        """Caller holds self.lock."""
        u = self.units.setdefault(amb_id, Unit(amb_id))
        cmd = p.get("command")
        if retained and cmd in ("FOLLOW_ROUTE", "ROUTE_UPDATED") and u.mission is None:
            # replayed by the broker after a restart (any resume block in it is stale): wait for the backend's
            # live answer to simulator/hello with the current resume point
            u.pending = ({k: v for k, v in p.items() if k != "resume"}, time.time())
            log.info(jmsg(event="ROUTE_HELD_FOR_RESUME", ambulance_id=amb_id, route_id=p.get("route_id")))
            return
        u.pending = None
        if cmd in ("FOLLOW_ROUTE", "ROUTE_UPDATED") and p.get("driver") == "DEVICE":
            u.mission = None          # a real GPS device drives this unit
            return
        if cmd in ("FOLLOW_ROUTE", "ROUTE_UPDATED"):
            segs = [Seg(s.get("road_id"), float(s["length_m"]), float(s["speed_kph"]), float(s.get("base_speed_kph", s["speed_kph"])),
                        [tuple(c) for c in s["coords"]]) for s in p["segments"] if s.get("coords")]
            if not segs:  # zero-length route (already at the destination)
                dest = tuple(p.get("dest") or p.get("origin") or (u.lat, u.lon))
                segs = [Seg(None, 0.0, 1.0, 1.0, [dest, dest])]
            if u.mission and u.mission.route_id == p["route_id"]:
                return  # duplicate (retained) delivery
            m = Mission(p["route_id"], p.get("leg", ""), float(p.get("time_scale", 1.0)), segs)
            if p.get("resume") and u.mission is None:
                if not self._resume(u, m, p["resume"]):
                    u.pending = ({k: v for k, v in p.items() if k != "resume"}, time.time())
                    return                  # never teleport: hold until a consistent route/position arrives
            elif cmd == "ROUTE_UPDATED" and u.lat is not None and u.mission is not None and not u.mission.arrived:
                # re-route of the current leg: continue from the CURRENT position (no teleport, no restart)
                k, pos, off = self.snap(segs, u.lat, u.lon)
                m.seg_idx, m.pos_in_seg, m.started = k, pos, True
                m.progress_m = sum(sg.length_m for sg in segs[:k]) + pos
                log.info(jmsg(event="ROUTE_UPDATED", ambulance_id=amb_id, route_id=p["route_id"],
                              replaces=p.get("replaces_route_id"), snapped_offset_m=round(off, 1),
                              start_segment=k, length_m=round(sum(sg.length_m for sg in segs))))
            else:
                u.lat, u.lon = segs[0].coords[0]
                log.info(jmsg(event="ROUTE_RECEIVED", ambulance_id=amb_id, route_id=p["route_id"], leg=p.get("leg"),
                              segments=len(segs), length_m=round(sum(s.length_m for s in segs))))
            u.mission = m
        elif cmd == "IDLE":
            u.mission = None
            u.speed_kph = 0.0

    def _resume(self, u: Unit, m: Mission, r: dict) -> bool:
        """Place the unit at the persisted progress point if it agrees with the last GPS fix (no teleport)."""
        try:
            lat, lon, progress = float(r["latitude"]), float(r["longitude"]), max(0.0, float(r.get("progress_m") or 0.0))
        except (KeyError, TypeError, ValueError):
            return False
        acc, k = 0.0, 0
        while k < len(m.segs) - 1 and acc + m.segs[k].length_m < progress:
            acc += m.segs[k].length_m
            k += 1
        pos = min(m.segs[k].length_m, progress - acc)
        plat, plon, heading = self.point_on(m.segs[k], pos)
        off = haversine_m(lat, lon, plat, plon)
        if off > RESUME_MAX_OFFSET_M:          # progress and GPS disagree: snap the GPS fix onto the route instead
            k, pos, off = self.snap(m.segs, lat, lon, search=len(m.segs))
            if off > RESUME_MAX_OFFSET_M:
                log.warning(jmsg(event="RESUME_REJECTED", ambulance_id=u.ambulance_id, route_id=m.route_id,
                                 offset_m=round(off, 1)))
                return False
            plat, plon, heading = self.point_on(m.segs[k], pos)
            progress = sum(sg.length_m for sg in m.segs[:k]) + pos
        m.seg_idx, m.pos_in_seg, m.progress_m, m.started = k, pos, progress, True
        u.lat, u.lon, u.heading = plat, plon, heading
        log.info(jmsg(event="ROUTE_RESUMED", ambulance_id=u.ambulance_id, route_id=m.route_id, leg=m.leg,
                      progress_m=round(progress, 1), offset_from_gps_m=round(off, 1), segment=k))
        return True

    def release_stale_pending(self) -> None:
        """No resume answer (older backend / no telemetry): start the held command as before, and say so."""
        now = time.time()
        for u in self.units.values():
            if u.pending and u.mission is None and now - u.pending[1] >= RESUME_WAIT_S:
                p, _ = u.pending
                u.pending = None
                log.warning(jmsg(event="RESUME_UNAVAILABLE", ambulance_id=u.ambulance_id, route_id=p.get("route_id"),
                                 detail="no resume point from the backend - route started from its origin"))
                self._apply_command(u.ambulance_id, p)

    def publish(self, topic: str, payload: dict, qos: int = 0) -> None:
        self.client.publish(topic, json.dumps(payload), qos=qos)
        self.published += 1

    # -------------------------------------------------------------- movement
    def road_speed_kph(self, seg: Seg) -> float:
        st = self.roads.get(seg.road_id) if seg.road_id else None
        if st is None:
            return seg.speed_kph if seg.speed_kph > 0 else seg.base_speed_kph
        if st.get("blocked"):
            return 0.0
        limit = float(st.get("speed_limit_kph") or seg.base_speed_kph)
        return limit * CONGESTION_FACTORS.get(st.get("congestion_level", "FREE"), 1.0) * float(st.get("incident_multiplier", 1.0))

    @staticmethod
    def snap(segs: list[Seg], lat: float, lon: float, search: int = 12) -> tuple[int, float, float]:
        """Closest point to (lat, lon) on the first `search` segments -> (segment index, metres into it, offset m)."""
        import math
        kx, ky = 111_320.0 * math.cos(math.radians(lat)), 110_540.0
        best = (0, 0.0, float("inf"))
        for k, sg in enumerate(segs[:search]):
            (alat, alon), (blat, blon) = sg.coords[0], sg.coords[-1]
            ax, ay, bx, by = (alon - lon) * kx, (alat - lat) * ky, (blon - lon) * kx, (blat - lat) * ky
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
            dist = math.hypot(ax + t * dx, ay + t * dy)
            if dist < best[2]:
                best = (k, t * sg.length_m, dist)
        return best

    @staticmethod
    def point_on(seg: Seg, dist: float) -> tuple[float, float, float]:
        """Interpolate along the segment polyline; returns lat, lon, heading."""
        pts = seg.coords
        if len(pts) == 1:
            return pts[0][0], pts[0][1], 0.0
        total = sum(haversine_m(*a, *b) for a, b in zip(pts, pts[1:])) or 1.0
        target = dist / seg.length_m * total if seg.length_m > 0 else total
        acc = 0.0
        for a, b in zip(pts, pts[1:]):
            d = haversine_m(*a, *b)
            if acc + d >= target or b == pts[-1]:
                f = 0.0 if d == 0 else min(1.0, (target - acc) / d)
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, bearing_deg(*a, *b)
            acc += d
        return pts[-1][0], pts[-1][1], 0.0

    def step_unit(self, u: Unit, dt_wall: float) -> list[tuple[str, dict, int]]:
        out: list[tuple[str, dict, int]] = []
        m = u.mission
        now_iso = datetime.now(timezone.utc).isoformat()
        if m is None or m.arrived:
            return out
        if not m.started:
            m.started = True
            out.append((f"ambulance/{u.ambulance_id}/status",
                        {"ambulance_id": u.ambulance_id, "event": "ROUTE_STARTED", "route_id": m.route_id, "leg": m.leg,
                         "timestamp": now_iso, "source": "SIMULATOR"}, 1))
        budget_s = dt_wall * m.time_scale        # simulated seconds available this tick
        speed_now = 0.0
        moved = 0.0
        while budget_s > 1e-9 and m.seg_idx < len(m.segs):
            seg = m.segs[m.seg_idx]
            spd = self.road_speed_kph(seg)
            if spd <= 0:
                if m.pos_in_seg <= SEGMENT_ENTRY_EPS and m.seg_idx > 0:
                    break                          # closure ahead: wait at the junction for re-routing
                spd = seg.base_speed_kph           # already on the road when it closed: allowed to leave
            v = spd / 3.6
            speed_now = spd
            left = seg.length_m - m.pos_in_seg
            if v * budget_s >= left:
                budget_s -= left / v if v > 0 else budget_s
                moved += left
                m.progress_m += left
                m.seg_idx += 1
                m.pos_in_seg = 0.0
            else:
                d = v * budget_s
                m.pos_in_seg += d
                m.progress_m += d
                moved += d
                budget_s = 0
        if m.seg_idx >= len(m.segs):
            u.lat, u.lon = m.segs[-1].coords[-1]
            m.arrived = True
            u.speed_kph = 0.0
        else:
            u.lat, u.lon, u.heading = self.point_on(m.segs[m.seg_idx], m.pos_in_seg)
            u.speed_kph = speed_now if moved > 0 else 0.0
        u.odometer_m += moved
        u.fuel = max(0.0, u.fuel - moved / 1000 * FUEL_PCT_PER_KM)
        out.append((f"ambulance/{u.ambulance_id}/location",
                    {"ambulance_id": u.ambulance_id, "latitude": round(u.lat, 7), "longitude": round(u.lon, 7),
                     "speed": round(u.speed_kph, 1), "heading": round(u.heading, 1), "timestamp": now_iso,
                     "route_id": m.route_id, "progress_m": round(m.progress_m, 2), "segment_index": m.seg_idx}, 0))
        if m.arrived:
            log.info(jmsg(event="ARRIVED", ambulance_id=u.ambulance_id, route_id=m.route_id, leg=m.leg))
            if m.leg == "TO_HOSPITAL" and u.fuel < 30:
                u.fuel = 100.0                     # refuel while at hospital
            out.append((f"ambulance/{u.ambulance_id}/status",
                        {"ambulance_id": u.ambulance_id, "event": "ARRIVED", "route_id": m.route_id, "leg": m.leg,
                         "timestamp": now_iso, "source": "SIMULATOR"}, 1))
        if time.time() - u.last_telemetry >= 5 or m.arrived:
            u.last_telemetry = time.time()
            out.append((f"ambulance/{u.ambulance_id}/telemetry",
                        {"ambulance_id": u.ambulance_id, "fuel_level": round(u.fuel, 2),
                         "odometer_km": round(u.odometer_m / 1000, 3), "engine_temp_c": round(85 + u.speed_kph / 10, 1),
                         "timestamp": now_iso}, 0))
        return out

    # -------------------------------------------------------------- main loop
    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        last = time.time()
        last_hb = 0.0
        log.info(jmsg(event="SIMULATOR_STARTED", tick_s=self.tick_s))
        try:
            while not stop.is_set():
                time.sleep(self.tick_s)
                now = time.time()
                dt = now - last
                last = now
                with self.lock:
                    self.release_stale_pending()
                    msgs = [m for u in list(self.units.values()) for m in self.step_unit(u, dt)]
                for topic, payload, qos in msgs:
                    self.publish(topic, payload, qos)
                if now - last_hb >= 2:
                    last_hb = now
                    active = sum(1 for u in self.units.values() if u.mission and not u.mission.arrived)
                    self.publish("simulator/heartbeat", {"component": "ambulance", "active_units": active,
                                                         "known_units": len(self.units), "published": self.published,
                                                         "ts": datetime.now(timezone.utc).isoformat()})
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    load_env()
    AmbulanceSimulator(os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", 1883)),
                       float(os.environ.get("SIM_TICK_S", 1.0))).run()
