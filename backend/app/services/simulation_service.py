"""Simulation mode (seeded, reproducible event schedules) and the scripted end-to-end demo scenario.

Both run inside the backend as asyncio tasks; all blocking work runs in worker threads. Ambulance
movement itself is performed by the external IoT simulator (simulator/ambulance_simulator.py) via MQTT.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from sqlalchemy import select, text

from app.config import get_settings
from app.database import session_scope
from app.ml.dataset import CONSCIOUSNESS, GRADE, generate
from app.models import Ambulance, EmergencyIncident, Hospital
from app.models.entities import point_wkt
from app.mqtt.client import publish
from app.services.dispatch_service import dispatch_incident, dispatcher_cycle
from app.services.events import broadcast_now, emit
from app.services.incident_service import create_incident
from app.services.mission_service import cancel_incident, set_amb_status
from app.services.routes_service import ACTIVE, position_on_route, remaining_eta
from app.services.state import STATE
from app.services.traffic_service import create_event, reset_all
from app.utils.geo import haversine_m
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.simulation")


@dataclass
class RunStatus:
    kind: str = "idle"                  # idle / simulation / scenario
    state: str = "IDLE"                 # IDLE / RUNNING / COMPLETED / FAILED / STOPPED
    seed: int | None = None
    started_at: float | None = None
    steps: list[dict] = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    error: str | None = None
    incident_id: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "state": self.state, "seed": self.seed, "started_at": self.started_at,
                "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else None,
                "steps": self.steps, "counters": self.counters, "error": self.error, "incident_id": self.incident_id,
                "simulator_connected": simulator_connected()}


RUN = RunStatus()
_task: asyncio.Task | None = None


def simulator_connected() -> bool:
    return STATE.simulator_last_seen is not None and time.time() - STATE.simulator_last_seen < 10


def _step(title: str, **details) -> None:
    RUN.steps.append({"n": len(RUN.steps) + 1, "title": title, "at": utcnow().isoformat(), "details": details})
    broadcast_now("SIMULATION_STATUS", RUN.as_dict())
    log_event(log, "SCENARIO_STEP", title, **{k: v for k, v in details.items() if not isinstance(v, (list, dict))})


# ------------------------------------------------------------------------------------------ fleet reset
def reset_operations(n_ambulances: int | None = None, n_hospitals: int | None = None) -> dict:
    """Cancel open incidents, return every unit to base, clear traffic. Keeps history for analytics."""
    with STATE.lock, session_scope() as db:
        open_incs = db.scalars(select(EmergencyIncident).where(
            EmergencyIncident.status.notin_(("COMPLETED", "CANCELLED")))).all()
        for inc in open_incs:
            cancel_incident(db, inc, by="simulation-reset")
        cleared = reset_all(db, source="SIMULATION")
        ambs = db.scalars(select(Ambulance).order_by(Ambulance.id)).all()
        for k, a in enumerate(ambs):
            a.latitude, a.longitude = a.base_latitude, a.base_longitude
            a.location = point_wkt(a.base_latitude, a.base_longitude)
            a.current_incident = None
            a.destination = a.destination_lat = a.destination_lon = None
            a.current_speed = 0
            a.missions_today = 0
            a.fuel_level = max(a.fuel_level, 60.0)
            active = n_ambulances is None or k < n_ambulances
            set_amb_status(db, a, "AVAILABLE" if active else "OFFLINE")
            publish(f"ambulance/{a.id}/command", {"command": "IDLE", "reset": True}, retain=True)
        hosp = db.scalars(select(Hospital).order_by(Hospital.id)).all()
        for k, h in enumerate(hosp):
            h.status = "ACTIVE" if n_hospitals is None or k < n_hospitals else "CLOSED"
        db.execute(text("UPDATE routes SET active=false WHERE active"))
        ACTIVE.clear()
        from app.services.telemetry_service import TELEMETRY
        TELEMETRY.latest.clear()
        emit(db, "SIMULATION_RESET", {"cancelled_incidents": len(open_incs), "cleared_roads": cleared,
                                      "active_ambulances": n_ambulances, "active_hospitals": n_hospitals})
    return {"cancelled_incidents": len(open_incs), "cleared_roads": cleared}


def random_case(rng: random.Random, row: dict) -> dict:
    return {"patient_age": int(row["age"]), "heart_rate": int(row["heart_rate"]), "respiratory_rate": int(row["respiratory_rate"]),
            "oxygen_saturation": int(row["oxygen_saturation"]), "consciousness": CONSCIOUSNESS[row["consciousness"]],
            "bleeding": GRADE[row["bleeding"]], "injury_severity": GRADE[row["injury_severity"]],
            "accident_type": row["accident_type"], "breathing_difficulty": bool(row["breathing_difficulty"]),
            "chest_pain": bool(row["chest_pain"]), "emergency_type": row["emergency_type"]}


def build_schedule(seed: int, n_incidents: int, n_traffic: int, duration_s: float) -> list[dict]:
    """Deterministic event schedule for a given seed (same seed → identical incidents & traffic events)."""
    s = get_settings()
    g = STATE.graph
    rng = random.Random(seed)
    cases = generate(n=max(1, n_incidents), seed=seed).to_dict("records")
    schedule = []
    candidates = [i for i in range(len(g.lat)) if haversine_m(s.city_lat, s.city_lon, g.lat[i], g.lon[i]) <= s.city_radius_m * 0.85]
    for k in range(n_incidents):
        node = candidates[rng.randrange(len(candidates))]
        schedule.append({"at": round(rng.uniform(0, duration_s), 2), "kind": "incident",
                         "lat": float(g.lat[node]), "lon": float(g.lon[node]), "case": random_case(rng, cases[k])})
    major = [r for r, hw in zip(g.road_ids, g.road_highway) if hw in ("primary", "secondary", "tertiary", "trunk")] or g.road_ids
    for _ in range(n_traffic):
        roll = rng.random()
        ev = "ACCIDENT" if roll < 0.2 else "BLOCK" if roll < 0.3 else "CONGESTION"
        level = rng.choice(["MODERATE", "HEAVY", "SEVERE"]) if ev == "CONGESTION" else None
        road = rng.choice(major)
        t = round(rng.uniform(0, duration_s), 2)
        schedule.append({"at": t, "kind": "traffic", "event_type": ev, "road_id": road, "level": level})
        if ev in ("ACCIDENT", "BLOCK"):  # incidents are cleared again later
            schedule.append({"at": round(min(duration_s * 1.2, t + rng.uniform(30, 90)), 2), "kind": "traffic",
                             "event_type": "CLEAR", "road_id": road, "level": None})
    schedule.sort(key=lambda e: (e["at"], e["kind"]))
    return schedule


def _exec_schedule_item(item: dict) -> None:
    with session_scope() as db:
        if item["kind"] == "incident":
            create_incident(db, {**item["case"], "latitude": item["lat"], "longitude": item["lon"]},
                            created_by="simulation", source="SIMULATION")
        else:
            create_event(db, item["event_type"], road_id=item["road_id"], level=item["level"], source="SIMULATION")


async def run_simulation(seed: int, n_amb: int, n_hosp: int, n_inc: int, n_traffic: int, duration_s: float) -> None:
    global RUN
    RUN = RunStatus(kind="simulation", state="RUNNING", seed=seed, started_at=time.time(),
                    counters={"incidents": 0, "traffic_events": 0, "planned_incidents": n_inc, "planned_traffic": n_traffic})
    try:
        await asyncio.to_thread(reset_operations, n_amb, n_hosp)
        schedule = await asyncio.to_thread(build_schedule, seed, n_inc, n_traffic, duration_s)
        _step("Simulation started", seed=seed, ambulances=n_amb, hospitals=n_hosp, incidents=n_inc,
              traffic_events=n_traffic, schedule_items=len(schedule))
        t0 = time.time()
        for item in schedule:
            delay = item["at"] - (time.time() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            await asyncio.to_thread(_exec_schedule_item, item)
            RUN.counters["incidents" if item["kind"] == "incident" else "traffic_events"] += 1
            if item["kind"] == "incident":
                await asyncio.to_thread(dispatcher_cycle)
            broadcast_now("SIMULATION_STATUS", RUN.as_dict())
        RUN.state = "COMPLETED"
        _step("All scheduled events injected", **RUN.counters)
    except asyncio.CancelledError:
        RUN.state = "STOPPED"
        _step("Simulation stopped")
        raise
    except Exception as exc:
        RUN.state, RUN.error = "FAILED", str(exc)
        log.exception("simulation failed")
        _step("Simulation failed", error=str(exc))


# ------------------------------------------------------------------------------------------ demo scenario
CRITICAL_CASE = {"emergency_type": "accident", "patient_age": 34, "heart_rate": 142, "respiratory_rate": 31,
                 "oxygen_saturation": 84, "consciousness": "UNRESPONSIVE", "bleeding": "SEVERE",
                 "injury_severity": "SEVERE", "accident_type": "ROAD", "breathing_difficulty": True, "chest_pain": False,
                 "notes": "SCRIPTED DEMO: multi-vehicle road accident"}


def _scenario_setup(seed: int) -> dict:
    """Pick an incident location and 3 ambulances (nearest is BASIC, plus ADVANCED and ICU further away)."""
    s = get_settings()
    g = STATE.graph
    rng = random.Random(seed)
    reset_operations()
    with STATE.lock, session_scope() as db:
        ambs = db.scalars(select(Ambulance).order_by(Ambulance.id)).all()
        by_eq = {lvl: [a for a in ambs if a.equipment_level == lvl] for lvl in ("BASIC", "ADVANCED", "ICU")}
        # choose an incident node where the nearest BASIC, ADVANCED and ICU units are all 1.2-4 km away
        pool = [i for i in range(len(g.lat)) if haversine_m(s.city_lat, s.city_lon, g.lat[i], g.lon[i]) <= s.city_radius_m * 0.8]
        rng.shuffle(pool)

        def pick(lat, lon, lo, hi):
            out = []
            for lvl in ("BASIC", "ADVANCED", "ICU"):
                ok = [a for a in by_eq[lvl] if lo <= haversine_m(a.base_latitude, a.base_longitude, lat, lon) <= hi]
                if not ok:
                    return None
                out.append(min(ok, key=lambda a: haversine_m(a.base_latitude, a.base_longitude, lat, lon)))
            return out
        chosen = None
        for lo, hi in ((1200, 4000), (800, 5000), (0, 1e9)):
            for i in pool[:1500]:
                lat, lon = float(g.lat[i]), float(g.lon[i])
                units = pick(lat, lon, lo, hi)
                if units:
                    chosen = (lat, lon, units)
                    break
            if chosen:
                break
        lat, lon, (basic, adv, icu) = chosen
        keep = {icu.id, basic.id, adv.id}
        for a in ambs:
            if a.id not in keep:
                set_amb_status(db, a, "OFFLINE")
        units = [{"id": a.id, "equipment": a.equipment_level,
                  "straight_line_m": round(haversine_m(a.latitude, a.longitude, lat, lon))} for a in (basic, adv, icu)]
    return {"lat": lat, "lon": lon, "units": units}


def _scenario_create(lat: float, lon: float) -> dict:
    with STATE.lock, session_scope() as db:
        inc = create_incident(db, {**CRITICAL_CASE, "latitude": lat, "longitude": lon, "address": "Scripted demo location"},
                              created_by="demo-scenario", source="SCENARIO")
        db.flush()
        info = {"incident_id": str(inc.id), "reference": inc.reference, "ml": inc.predicted_severity,
                "ml_confidence": inc.ml_confidence, "rule_score": inc.rule_score, "severity": inc.severity,
                "priority": inc.priority, "required_capability": inc.required_capability}
    return info


def _scenario_dispatch(incident_id: str) -> dict:
    import uuid
    with STATE.lock, session_scope() as db:
        inc = db.get(EmergencyIncident, uuid.UUID(incident_id))
        d = dispatch_incident(db, inc, by="demo-scenario")
        return {"ambulance_id": d.ambulance_id, "score": round(d.dispatch_score, 4),
                "eta_s": round(d.eta_to_patient_s, 1), "candidates": d.candidates, "explanation": d.explanation}


def _scenario_inject_accident(ambulance_id: str) -> dict | None:
    """Accident on a road the ambulance has not reached yet (middle of its remaining route)."""
    ar = ACTIVE.get(ambulance_id)
    if ar is None:
        return None
    router = STATE.router
    rem, _ = router.remaining(ar.segments, ar.progress_m)
    ahead = [s for s in rem[2:-2] if s.road_id] or [s for s in rem[1:] if s.road_id]
    if not ahead:
        return None
    lengths: dict[str, float] = {}
    for seg in ahead[: max(1, int(len(ahead) * 0.7))]:
        lengths[seg.road_id] = lengths.get(seg.road_id, 0) + seg.length_m
    # Script choice only: probe the longest roads ahead and prefer one where an accident would leave a
    # usable detour, so the demo shows a finite "old ETA -> new ETA". The probe state is restored at once;
    # the actual re-routing decision is made later by the normal route monitor.
    g = STATE.graph
    road, best_gain = max(lengths, key=lengths.get), -1.0
    k, frac, point = position_on_route(ar, ar.progress_m)
    nxt = ar.segments[k].to_node
    for cand in sorted(lengths, key=lengths.get, reverse=True)[:8]:
        st = g.road_state(cand)
        with g.lock:
            g.set_road_state(cand, "SEVERE", 0.5, False)
            try:
                degraded = remaining_eta(ar)
                alt = router.route(point, ar.dest, origin_node=g.idx_of.get(nxt), compute_shortest=False) \
                    if nxt in g.idx_of else None
                gain = degraded - alt.adjusted_duration_s if alt and cand not in alt.road_ids() else -1.0
            except Exception:
                gain = -1.0
            finally:
                g.set_road_state(cand, st.level, st.incident_multiplier, st.blocked)
        if gain > best_gain:
            road, best_gain = cand, gain
    before = remaining_eta(ar)
    with session_scope() as db:
        create_event(db, "ACCIDENT", road_id=road, source="SCENARIO")
    return {"road_id": road, "eta_before_s": round(before, 1), "route_id": str(ar.route_id)}


async def _wait_for(pred, timeout_s: float, poll: float = 0.5):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        res = await asyncio.to_thread(pred)
        if res:
            return res
        await asyncio.sleep(poll)
    return None


def _incident_status(incident_id: str) -> str | None:
    import uuid
    with session_scope() as db:
        inc = db.get(EmergencyIncident, uuid.UUID(incident_id))
        return inc.status if inc else None


def _last_reroute(incident_id: str):
    with session_scope() as db:
        row = db.execute(text("SELECT reroute_reason, old_eta_s, adjusted_duration_s, time_saved_s, engine FROM routes "
                              "WHERE incident_id = :i AND reroute_of IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
                         {"i": incident_id}).first()
        return dict(row._mapping) if row else None


async def run_demo_scenario(seed: int = 42) -> None:
    global RUN
    RUN = RunStatus(kind="scenario", state="RUNNING", seed=seed, started_at=time.time())
    s = get_settings()
    try:
        if STATE.router is None:
            raise RuntimeError("routing engine not ready")
        setup = await asyncio.to_thread(_scenario_setup, seed)
        _step("1. Three ambulances available (all other units set OFFLINE)", units=setup["units"])
        inc = await asyncio.to_thread(_scenario_create, setup["lat"], setup["lon"])
        RUN.incident_id = inc["incident_id"]
        _step("2. Critical road accident reported", reference=inc["reference"], lat=setup["lat"], lon=setup["lon"])
        _step(f"3. ML predicts {inc['ml']} (confidence {inc['ml_confidence']:.2f}); rule score {inc['rule_score']}",
              **inc)
        disp = await asyncio.to_thread(_scenario_dispatch, inc["incident_id"])
        _step(f"4. {len(disp['candidates'])} candidate ambulances evaluated", candidates=[
            {k: c[k] for k in ("ambulance_id", "equipment_level", "eta_s", "distance_m", "score", "suitable")}
            for c in disp["candidates"]])
        _step(f"5. {disp['ambulance_id']} selected (score {disp['score']}, ETA {disp['eta_s'] / 60:.1f} min)",
              explanation=disp["explanation"])
        if not simulator_connected():
            _step("WAITING: ambulance simulator not detected - start it with `python simulator/run_simulator.py`")
        amb = disp["ambulance_id"]

        def moving():
            ar = ACTIVE.get(amb)
            return ar is not None and ar.total_m > 0 and ar.progress_m / ar.total_m >= 0.15
        if not await _wait_for(moving, 180):
            raise RuntimeError("ambulance did not start moving - is the simulator running and connected to MQTT?")
        ar = ACTIVE.get(amb)
        _step("6. Ambulance moving (live MQTT location updates)", progress_m=round(ar.progress_m),
              route_length_m=round(ar.total_m))
        acc = await asyncio.to_thread(_scenario_inject_accident, amb)
        if acc is None:
            raise RuntimeError("could not find a road ahead of the ambulance for the accident")
        ar2 = ACTIVE.get(amb)
        degraded = None
        if ar2 is not None and str(ar2.route_id) == acc["route_id"]:
            degraded = round(remaining_eta(ar2), 1)
        _step(f"7. Traffic accident injected on {acc['road_id']} (on current route)", **acc,
              eta_with_accident_s=degraded)
        rr = await _wait_for(lambda: _last_reroute(inc["incident_id"]), 15)
        if not rr:
            # the accident alone did not justify a new route: close the road completely
            with session_scope() as db:
                create_event(db, "BLOCK", road_id=acc["road_id"], source="SCENARIO")
            _step(f"7b. No faster alternative under SEVERE congestion - road {acc['road_id']} now BLOCKED")
            rr = await _wait_for(lambda: _last_reroute(inc["incident_id"]), 15)
        if rr:
            old, new = rr["old_eta_s"], rr["adjusted_duration_s"]
            _step("8-11. ROUTE RECALCULATED: " + (f"Old ETA {old / 60:.1f} min → New ETA {new / 60:.1f} min, "
                  f"time saved {rr['time_saved_s'] / 60:.1f} min" if old else f"road blocked → new ETA {new / 60:.1f} min"),
                  **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in rr.items()})
        else:
            _step("8-11. Route check completed: no faster alternative existed (current route kept)")
        if not await _wait_for(lambda: _incident_status(inc["incident_id"]) in ("ARRIVED", "PATIENT_LOADED", "TO_HOSPITAL", "COMPLETED"), 600):
            raise RuntimeError("ambulance did not reach the patient in time")
        _step("12. Ambulance reached the patient")
        if not await _wait_for(lambda: _incident_status(inc["incident_id"]) in ("TO_HOSPITAL", "COMPLETED"),
                               s.scene_time_s / s.sim_time_scale + 60):
            raise RuntimeError("patient was not loaded")
        with session_scope() as db:
            row = db.execute(text("SELECT d.hospital_id, h.name, d.hospital_explanation FROM dispatches d "
                                  "JOIN hospitals h ON h.id = d.hospital_id WHERE d.incident_id = :i"),
                             {"i": inc["incident_id"]}).first()
        _step(f"13. Hospital selected: {row[1]}", hospital_id=row[0], explanation=row[2])
        _step("14. Ambulance transporting patient to hospital")
        if not await _wait_for(lambda: _incident_status(inc["incident_id"]) == "COMPLETED", 900):
            raise RuntimeError("incident did not complete")
        with session_scope() as db:
            r = db.execute(text(
                "SELECT extract(epoch FROM arrived_at-created_at)*:k, extract(epoch FROM completed_at-created_at)*:k "
                "FROM emergency_incidents WHERE id=:i"), {"i": inc["incident_id"], "k": s.sim_time_scale}).first()
            n_events = db.scalar(text("SELECT count(*) FROM system_events WHERE incident_id=:i"), {"i": inc["incident_id"]})
        _step("15. Incident completed", response_time_s=round(r[0], 1), total_time_s=round(r[1], 1))
        with session_scope() as db:
            create_event(db, "CLEAR", road_id=acc["road_id"], source="SCENARIO")
        _step("16. Analytics updated (scenario traffic cleared)", stored_events=n_events)
        RUN.state = "COMPLETED"
        broadcast_now("SIMULATION_STATUS", RUN.as_dict())
    except asyncio.CancelledError:
        RUN.state = "STOPPED"
        raise
    except Exception as exc:
        RUN.state, RUN.error = "FAILED", str(exc)
        log.exception("demo scenario failed")
        _step("Scenario failed", error=str(exc))


def start_task(coro) -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = asyncio.get_running_loop().create_task(coro)


def stop_task() -> bool:
    if _task and not _task.done():
        _task.cancel()
        return True
    return False


