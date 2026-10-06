"""Predictive hospital congestion - a transparent ESTIMATE built only from data the system has.

    predicted_load(h) = L0 * exp(-h / stay)            patients still in the ED after h minutes
                      + incoming(h)                    ambulances already transporting to it, arriving within h
                      + lambda * h                     new ambulance assignments expected (observed rate)
    expected_wait     = slot * rho / (1 - rho),  rho = min(predicted_load / capacity, 0.98)   (M/M/1-style)

  L0       hospitals.current_load (actual)
  incoming active TO_HOSPITAL routes with their live remaining ETA (actual)
  lambda   HOSPITAL_SELECTED events for the hospital within HOSPITAL_RATE_WINDOW_MIN (observed)
  stay     mean ED length of stay estimated from observed discharges: stay = mean_load / discharge_rate;
           when fewer than 3 discharges were observed: ED_MEAN_STAY_MIN (configured default)
  slot     ED_TREATMENT_SLOT_MIN (configured)
No real hospital waiting-time feed exists in this project, so every output is labelled ESTIMATED together
with the method (OBSERVED_RATES / DEFAULT_RATES) and the number of data points behind it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import session_scope
from app.models import Hospital
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.hospital_prediction")
MODEL_VERSION = "hospital-queue-v1"


@dataclass
class HospitalForecast:
    hospital_id: str
    horizon_min: float
    current_load: int
    capacity: int
    incoming_ambulances: int
    predicted_load: float
    predicted_load_pct: float
    expected_wait_min: float
    arrival_rate_per_h: float
    mean_stay_min: float
    method: str
    data_points: int
    confidence: str
    model_version: str = MODEL_VERSION
    estimated: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def expected_wait_min(load: float, capacity: int, slot_min: float, max_wait: float) -> float:
    rho = min(max(load, 0.0) / max(capacity, 1), 0.98)
    return round(min(max_wait, slot_min * rho / (1 - rho)), 2)


def forecast(current_load: int, capacity: int, horizon_min: float, incoming_etas_min: list[float],
             arrivals: int, discharges: int, window_min: float, default_stay_min: float, slot_min: float,
             max_wait: float, hospital_id: str = "") -> HospitalForecast:
    """Pure function (unit-tested)."""
    lam_per_min = arrivals / window_min if window_min > 0 else 0.0
    if discharges >= 3 and current_load > 0:
        rate_per_patient = discharges / (max(current_load, 1) * window_min)
        stay = min(1440.0, max(30.0, 1 / rate_per_patient))
        method = "OBSERVED_RATES"
    else:
        stay = default_stay_min
        method = "OBSERVED_RATES" if arrivals >= 3 else "DEFAULT_RATES"
    incoming = sum(1 for e in incoming_etas_min if e <= horizon_min)
    pred = current_load * math.exp(-horizon_min / stay) + incoming + lam_per_min * horizon_min
    points = arrivals + discharges
    conf = "HIGH" if points >= 20 else "MEDIUM" if points >= 5 else "LOW"
    return HospitalForecast(hospital_id, horizon_min, current_load, capacity, incoming, round(pred, 2),
                            round(100 * pred / max(capacity, 1), 1), expected_wait_min(pred, capacity, slot_min, max_wait),
                            round(lam_per_min * 60, 3), round(stay, 1), method, points, conf)


def _observations(db: Session, window_min: float) -> tuple[dict, dict, dict]:
    """Per hospital: observed arrivals, discharges (within the window) and live incoming ETAs (minutes)."""
    st = get_settings()
    wall_s = window_min * 60 / st.sim_time_scale
    arrivals = dict(db.execute(text(
        "SELECT payload->>'hospital_id', count(*) FROM system_events WHERE event_type='HOSPITAL_SELECTED' "
        "AND created_at > now() - make_interval(secs => :s) GROUP BY 1"), {"s": wall_s}).all())
    discharges = dict(db.execute(text(
        "SELECT payload->>'hospital_id', count(*) FROM system_events WHERE event_type='HOSPITAL_DISCHARGE' "
        "AND created_at > now() - make_interval(secs => :s) GROUP BY 1"), {"s": wall_s}).all())
    from app.services.routes_service import ACTIVE, remaining_eta
    incoming: dict[str, list[float]] = {}
    rows = db.execute(text("SELECT a.id, i.destination_hospital FROM emergency_incidents i "
                           "JOIN ambulances a ON a.current_incident = i.id WHERE i.status = 'TO_HOSPITAL' "
                           "AND i.hospital_arrived_at IS NULL")).all()
    for amb, hid in rows:
        ar = ACTIVE.get(amb)
        if ar is not None and ar.leg == "TO_HOSPITAL" and hid:
            incoming.setdefault(hid, []).append(remaining_eta(ar) / 60)
    return arrivals, discharges, incoming


def predict_hospitals(db: Session, horizon_min: float | None = None,
                      horizons_by_hospital: dict[str, float] | None = None) -> dict[str, HospitalForecast]:
    st = get_settings()
    arrivals, discharges, incoming = _observations(db, st.hospital_rate_window_min)
    out = {}
    for h in db.scalars(select(Hospital).where(Hospital.status != "CLOSED")):
        hz = (horizons_by_hospital or {}).get(h.id, horizon_min if horizon_min is not None else st.hospital_prediction_horizon_min)
        out[h.id] = forecast(h.current_load, h.emergency_capacity, hz, incoming.get(h.id, []), int(arrivals.get(h.id, 0)),
                             int(discharges.get(h.id, 0)), st.hospital_rate_window_min, st.ed_mean_stay_min,
                             st.ed_treatment_slot_min, st.hospital_max_wait_min, h.id)
    return out


def store(db: Session, forecasts: dict[str, HospitalForecast]) -> None:
    rows = [{k: v for k, v in f.as_dict().items() if k != "estimated"} | {"prediction_time": utcnow()}
            for f in forecasts.values()]
    if not rows:
        return
    db.execute(text(
        "INSERT INTO hospital_predictions(hospital_id, horizon_min, prediction_time, current_load, capacity, "
        "incoming_ambulances, predicted_load, predicted_load_pct, expected_wait_min, arrival_rate_per_h, mean_stay_min, "
        "method, data_points, confidence, model_version) VALUES (:hospital_id, :horizon_min, :prediction_time, "
        ":current_load, :capacity, :incoming_ambulances, :predicted_load, :predicted_load_pct, :expected_wait_min, "
        ":arrival_rate_per_h, :mean_stay_min, :method, :data_points, :confidence, :model_version) "
        "ON CONFLICT (hospital_id, horizon_min) DO UPDATE SET prediction_time=EXCLUDED.prediction_time, "
        "current_load=EXCLUDED.current_load, capacity=EXCLUDED.capacity, incoming_ambulances=EXCLUDED.incoming_ambulances, "
        "predicted_load=EXCLUDED.predicted_load, predicted_load_pct=EXCLUDED.predicted_load_pct, "
        "expected_wait_min=EXCLUDED.expected_wait_min, arrival_rate_per_h=EXCLUDED.arrival_rate_per_h, "
        "mean_stay_min=EXCLUDED.mean_stay_min, method=EXCLUDED.method, data_points=EXCLUDED.data_points, "
        "confidence=EXCLUDED.confidence, model_version=EXCLUDED.model_version"), rows)


def hospital_cycle() -> None:
    with session_scope() as db:
        f = predict_hospitals(db)
        store(db, f)
    log_event(log, "HOSPITAL_CONGESTION_PREDICTED", hospitals=len(f), model_version=MODEL_VERSION,
              horizon_min=get_settings().hospital_prediction_horizon_min)
