"""Analytics computed from stored incidents, dispatches, routes and events (nothing is hard-coded).

All durations are SIMULATED seconds: wall-clock interval × SIM_TIME_SCALE. Rows with
source = HISTORICAL_SEED are synthetic seed data; their response times were computed at seed time
from graph-routed ETAs and are reported separately from live/simulation incidents.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings

OPEN = "('CREATED','CLASSIFYING','WAITING','DISPATCHED','EN_ROUTE','ARRIVED','PATIENT_LOADED','TO_HOSPITAL')"


def _scalar(db: Session, sql: str, **p):
    return db.execute(text(sql), p).scalar()


def summary(db: Session) -> dict:
    k = get_settings().sim_time_scale
    amb = dict(db.execute(text("SELECT status, count(*) FROM ambulances GROUP BY status")).all())
    in_transit = sum(amb.get(s, 0) for s in ("DISPATCHED", "EN_ROUTE_TO_PATIENT", "TRANSPORTING"))
    busy = in_transit + amb.get("AT_SCENE", 0) + amb.get("AT_HOSPITAL", 0)
    fleet = sum(amb.values()) - amb.get("OFFLINE", 0) - amb.get("MAINTENANCE", 0)
    live = "source <> 'HISTORICAL_SEED'"
    resp = db.execute(text(
        f"SELECT avg(extract(epoch FROM arrived_at - created_at)) * :k, count(*) FROM emergency_incidents "
        f"WHERE {live} AND arrived_at IS NOT NULL"), {"k": k}).first()
    disp = db.execute(text(
        f"SELECT avg(extract(epoch FROM dispatched_at - created_at)) * :k, count(*) FROM emergency_incidents "
        f"WHERE {live} AND dispatched_at IS NOT NULL"), {"k": k}).first()
    hist = db.execute(text(
        "SELECT avg(historical_response_s), avg(historical_dispatch_s), count(*) FROM emergency_incidents "
        "WHERE source = 'HISTORICAL_SEED'")).first()
    reroutes = db.execute(text(
        "SELECT count(*), coalesce(sum(time_saved_s),0), avg(time_saved_s) FROM routes WHERE reroute_of IS NOT NULL")).first()
    eff = db.execute(text(
        "SELECT avg(shortest_distance_m / NULLIF(distance_m,0)), count(*) FROM routes "
        "WHERE leg <> 'PREVIEW' AND shortest_distance_m IS NOT NULL")).first()
    return {
        "time_basis": f"simulated seconds (wall-clock x {k})",
        "active_emergencies": _scalar(db, f"SELECT count(*) FROM emergency_incidents WHERE status IN {OPEN}"),
        "waiting_emergencies": _scalar(db, "SELECT count(*) FROM emergency_incidents WHERE status = 'WAITING'"),
        "critical_active": _scalar(db, f"SELECT count(*) FROM emergency_incidents WHERE status IN {OPEN} AND severity='CRITICAL'"),
        "incidents_today": _scalar(db, "SELECT count(*) FROM emergency_incidents WHERE created_at >= date_trunc('day', now())"),
        "critical_today": _scalar(db, "SELECT count(*) FROM emergency_incidents WHERE created_at >= date_trunc('day', now()) AND severity='CRITICAL'"),
        "completed_live": _scalar(db, f"SELECT count(*) FROM emergency_incidents WHERE {live} AND status='COMPLETED'"),
        "ambulances_by_status": amb,
        "available_ambulances": amb.get("AVAILABLE", 0),
        "ambulances_in_transit": in_transit,
        "fleet_utilization_pct": round(100 * busy / fleet, 1) if fleet else 0.0,
        "hospitals": _scalar(db, "SELECT count(*) FROM hospitals WHERE status <> 'CLOSED'"),
        "avg_hospital_load_pct": _scalar(db, "SELECT round(avg(100.0*current_load/emergency_capacity)::numeric,1) FROM hospitals WHERE status <> 'CLOSED'"),
        "avg_response_time_s": None if resp[0] is None else round(resp[0], 1), "response_samples": resp[1],
        "avg_dispatch_time_s": None if disp[0] is None else round(disp[0], 1), "dispatch_samples": disp[1],
        "avg_decision_ms": _scalar(db, "SELECT round(avg(decision_ms)::numeric,1) FROM dispatches"),
        "historical_seed": {"incidents": hist[2], "avg_response_time_s": None if hist[0] is None else round(hist[0], 1),
                            "avg_dispatch_time_s": None if hist[1] is None else round(hist[1], 1)},
        "reroutes": reroutes[0], "reroute_time_saved_s": round(reroutes[1], 1),
        "avg_time_saved_s": None if reroutes[2] is None else round(reroutes[2], 1),
        "avg_route_efficiency": None if eff[0] is None else round(eff[0], 3), "routes_measured": eff[1],
        "traffic": dict(db.execute(text("SELECT congestion_level, count(*) FROM road_conditions GROUP BY 1")).all()),
    }


def breakdowns(db: Session) -> dict:
    return {
        "by_type": [dict(r._mapping) for r in db.execute(text(
            "SELECT emergency_type AS key, count(*) AS count FROM emergency_incidents GROUP BY 1 ORDER BY 2 DESC"))],
        "by_severity": [dict(r._mapping) for r in db.execute(text(
            "SELECT coalesce(severity,'UNCLASSIFIED') AS key, count(*) AS count, "
            "count(*) FILTER (WHERE source='HISTORICAL_SEED') AS historical FROM emergency_incidents GROUP BY 1"))],
        "by_status": [dict(r._mapping) for r in db.execute(text(
            "SELECT status AS key, count(*) AS count FROM emergency_incidents GROUP BY 1"))],
        "ml_vs_rule": [dict(r._mapping) for r in db.execute(text(
            "SELECT predicted_severity AS ml, rule_severity AS rule, count(*) AS count FROM emergency_incidents "
            "WHERE predicted_severity IS NOT NULL GROUP BY 1,2"))],
        "ambulance_utilization": [dict(r._mapping) for r in db.execute(text(
            "SELECT a.id AS ambulance_id, a.equipment_level, a.status, a.missions_today, "
            "count(d.id) AS dispatches_total, round(a.fuel_level::numeric,1) AS fuel_level "
            "FROM ambulances a LEFT JOIN dispatches d ON d.ambulance_id = a.id GROUP BY a.id ORDER BY a.id"))],
        "hospital_load": [dict(r._mapping) for r in db.execute(text(
            "SELECT id, name, current_load, emergency_capacity, icu_available, "
            "round(100.0*current_load/emergency_capacity,1) AS load_pct FROM hospitals ORDER BY id"))],
        "dispatch_methods": [dict(r._mapping) for r in db.execute(text(
            "SELECT method AS key, count(*) AS count FROM dispatches GROUP BY 1"))],
    }


def response_times(db: Session, limit: int = 200) -> dict:
    k = get_settings().sim_time_scale
    rows = db.execute(text(
        "SELECT i.reference, i.emergency_type, i.severity, i.source, i.created_at, "
        "CASE WHEN i.source='HISTORICAL_SEED' THEN i.historical_response_s "
        "     ELSE extract(epoch FROM i.arrived_at - i.created_at) * :k END AS response_s, "
        "CASE WHEN i.source='HISTORICAL_SEED' THEN i.historical_dispatch_s "
        "     ELSE extract(epoch FROM i.dispatched_at - i.created_at) * :k END AS dispatch_s, "
        "d.eta_to_patient_s AS planned_eta_s, d.decision_ms "
        "FROM emergency_incidents i LEFT JOIN dispatches d ON d.incident_id = i.id AND d.status <> 'CANCELLED' "
        "WHERE (i.arrived_at IS NOT NULL OR i.source='HISTORICAL_SEED') ORDER BY i.created_at DESC LIMIT :n"),
        {"k": k, "n": limit}).all()
    items = [dict(r._mapping) for r in rows]
    by_sev = db.execute(text(
        "SELECT severity, round(avg(CASE WHEN source='HISTORICAL_SEED' THEN historical_response_s "
        "ELSE extract(epoch FROM arrived_at - created_at) * :k END)::numeric,1) AS avg_response_s, count(*) "
        "FROM emergency_incidents WHERE arrived_at IS NOT NULL OR source='HISTORICAL_SEED' GROUP BY 1"), {"k": k}).all()
    return {"time_basis": f"simulated seconds (wall-clock x {k})", "items": items,
            "by_severity": [dict(r._mapping) for r in by_sev]}


def reroutes(db: Session, limit: int = 50) -> list[dict]:
    rows = db.execute(text(
        "SELECT r.id, r.created_at, r.ambulance_id, r.leg, r.reroute_reason, r.old_eta_s, r.adjusted_duration_s AS new_eta_s, "
        "r.time_saved_s, r.engine, i.reference FROM routes r LEFT JOIN emergency_incidents i ON i.id = r.incident_id "
        "WHERE r.reroute_of IS NOT NULL ORDER BY r.created_at DESC LIMIT :n"), {"n": limit}).all()
    return [dict(r._mapping) for r in rows]
