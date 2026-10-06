"""Traffic prediction service: trains the short-horizon model (app/ml/traffic_model.py) on the stored traffic
history, predicts the level of the relevant roads H minutes ahead, stores the predictions (traffic_predictions)
and pushes predicted speeds into the routing graph, where the router uses them for time-dependent ETAs.

Relevant roads = roads not FREE now, roads with a change in the last H minutes, roads on active routes.
All other roads are predicted to keep their current state (no row stored)."""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text

from app.config import get_settings
from app.database import get_engine
from app.ml.traffic_model import LEVELS, TrafficModel, build_histories, hw_index, to_sim_min, train
from app.routing.traffic import CONGESTION_FACTORS
from app.services.events import broadcast_now
from app.services.state import STATE
from app.utils.logging import log_event
from app.utils.timeutil import utcnow

log = logging.getLogger("app.traffic_prediction")


class TrafficPredictor:
    def __init__(self):
        self.model: TrafficModel | None = None
        self.trained_at: float | None = None
        self.last_cycle: dict = {}
        self._lock = threading.Lock()
        self._hist_events = 0

    # ------------------------------------------------------------------ training
    def _history(self, conn, days: float = 7.0):
        return conn.execute(text(
            "SELECT road_id, created_at, new_level, blocked, event_type FROM traffic_events "
            "WHERE road_id IS NOT NULL AND new_level IS NOT NULL AND created_at > now() - make_interval(secs => :s) "
            "ORDER BY road_id, created_at"), {"s": days * 86400}).all()

    def retrain(self) -> TrafficModel:
        st = get_settings()
        with get_engine().connect() as conn:
            rows = self._history(conn)
            roads = {r[0]: (r[1], float(r[2])) for r in conn.execute(text(
                "SELECT road_id, highway_type, speed_limit_kph FROM road_conditions WHERE road_id IN "
                "(SELECT DISTINCT road_id FROM traffic_events WHERE road_id IS NOT NULL)"))}
        hist = build_histories(rows, st.sim_time_scale)
        model = train(hist, roads, st.traffic_prediction_horizon_min, st.sim_time_scale,
                      to_sim_min(utcnow(), st.sim_time_scale), st.traffic_model_min_samples)
        with self._lock:
            self.model, self.trained_at, self._hist_events = model, time.time(), len(rows)
        log_event(log, "TRAFFIC_MODEL_TRAINED", model_version=model.version, method=model.method, **model.metrics)
        return model

    def ensure_model(self, force: bool = False) -> TrafficModel:
        st = get_settings()
        if force or self.model is None or self.trained_at is None or time.time() - self.trained_at > st.traffic_model_retrain_s \
                or self.model.horizon_min != st.traffic_prediction_horizon_min:
            return self.retrain()
        return self.model

    # ------------------------------------------------------------------ prediction
    def predict(self, road_ids: set[str] | None = None) -> list[dict]:
        """Predict relevant roads (or the given ones), store and apply. Returns stored rows."""
        st = get_settings()
        if not st.traffic_prediction_enabled or STATE.graph is None:
            return []
        model = self.ensure_model()
        g = STATE.graph
        H = st.traffic_prediction_horizon_min
        full_cycle = road_ids is None
        with get_engine().connect() as conn:
            if road_ids is None:
                from app.services.routes_service import ACTIVE
                route_roads = {s.road_id for ar in ACTIVE.all() for s in ar.segments if s.road_id}
                recent = {r[0] for r in conn.execute(text(
                    "SELECT DISTINCT road_id FROM traffic_events WHERE road_id IS NOT NULL "
                    "AND created_at > now() - make_interval(secs => :s)"), {"s": H * 60 / st.sim_time_scale})}
                congested = {r[0] for r in conn.execute(text(
                    "SELECT road_id FROM road_conditions WHERE congestion_level <> 'FREE' OR blocked"))}
                road_ids = congested | recent | route_roads
            if not road_ids:
                rows = []
            else:
                rows = conn.execute(text(
                    "SELECT r.road_id, r.congestion_level, r.blocked, r.incident_multiplier, r.highway_type, r.speed_limit_kph,"
                    " r.current_speed_kph, r.length_m, r.updated_at, "
                    " (SELECT e.event_type FROM traffic_events e WHERE e.road_id = r.road_id ORDER BY e.created_at DESC LIMIT 1),"
                    " (SELECT count(*) FROM traffic_events e WHERE e.road_id = r.road_id) "
                    "FROM road_conditions r WHERE r.road_id = ANY(:ids)"), {"ids": list(road_ids)}).all()
        now = utcnow()
        inputs = []
        for r in rows:
            age_min = max(0.0, (now - r[8]).total_seconds() * st.sim_time_scale / 60) if r[8] else 0.0
            inputs.append({"road_id": r[0], "cur": LEVELS.index(r[1]), "blocked": bool(r[2]),
                           "accident": r[9] == "ACCIDENT" and r[1] != "FREE", "age_min": age_min,
                           "hw": hw_index(r[4]), "limit": float(r[5]), "wall": now, "rate": 0.0})
        preds = model.predict(inputs)
        out = []
        for r, inp, (lvl_i, conf, method) in zip(rows, inputs, preds):
            level = LEVELS[lvl_i]
            mult = float(r[3]) if inp["accident"] and level in ("SEVERE", "HEAVY") else 1.0
            pred_speed = 0.0 if level == "BLOCKED" else float(r[5]) * CONGESTION_FACTORS[level] * mult
            cur_speed = float(r[6])
            length = float(r[7])
            t_cur = length / (cur_speed / 3.6) if cur_speed > 0 else None
            t_pred = length / (pred_speed / 3.6) if pred_speed > 0 else None
            delay = (t_pred - t_cur) if t_cur is not None and t_pred is not None else (
                0.0 if t_cur is None and t_pred is None else (-1.0 if t_pred is not None else 1e6))
            out.append({"road_id": r[0], "horizon_min": H, "prediction_time": now, "current_level": r[1],
                        "predicted_level": level, "current_speed_kph": round(cur_speed, 2),
                        "predicted_speed_kph": round(pred_speed, 2), "predicted_delay_s": round(delay, 1),
                        "confidence": round(float(conf), 3), "method": method, "model_version": model.version,
                        "_mult": mult})
        changed = sum(1 for o in out if o["predicted_level"] != o["current_level"])
        if full_cycle:            # single-road refreshes (after a traffic change) do not overwrite the cycle summary
            self.last_cycle = {"at": now.isoformat(), "roads": len(out), "predicted_changes": changed,
                               "model_version": model.version, "method": model.method, "horizon_min": H}
        self._store_and_apply(out, road_ids_full=None if full_cycle else road_ids)
        log_event(log, "TRAFFIC_PREDICTION_CREATED", roads=len(out), predicted_changes=changed,
                  model_version=model.version, method=model.method, horizon_min=H)
        return out

    def _store_and_apply(self, rows: list[dict], road_ids_full: set[str]) -> None:
        g = STATE.graph
        with get_engine().begin() as conn:
            if rows:
                conn.execute(text(
                    "INSERT INTO traffic_predictions(road_id, horizon_min, prediction_time, current_level, predicted_level, "
                    "current_speed_kph, predicted_speed_kph, predicted_delay_s, confidence, method, model_version) "
                    "VALUES (:road_id, :horizon_min, :prediction_time, :current_level, :predicted_level, :current_speed_kph,"
                    " :predicted_speed_kph, :predicted_delay_s, :confidence, :method, :model_version) "
                    "ON CONFLICT (road_id, horizon_min) DO UPDATE SET prediction_time=EXCLUDED.prediction_time, "
                    "current_level=EXCLUDED.current_level, predicted_level=EXCLUDED.predicted_level, "
                    "current_speed_kph=EXCLUDED.current_speed_kph, predicted_speed_kph=EXCLUDED.predicted_speed_kph, "
                    "predicted_delay_s=EXCLUDED.predicted_delay_s, confidence=EXCLUDED.confidence, method=EXCLUDED.method, "
                    "model_version=EXCLUDED.model_version"), [{k: v for k, v in r.items() if k != "_mult"} for r in rows])
            # predictions older than 3 cycles are stale: drop them (road predicted to keep its state)
            stale = conn.execute(text(
                "DELETE FROM traffic_predictions WHERE prediction_time < now() - make_interval(secs => :s) RETURNING road_id"),
                {"s": max(90.0, 3 * get_settings().traffic_prediction_interval_s)}).all()
        for r in rows:
            g.set_road_prediction(r["road_id"], r["predicted_level"], r["_mult"])
        for (rid,) in stale:
            g.set_road_prediction(rid, None)
        if rows and road_ids_full is None:
            broadcast_now("TRAFFIC_PREDICTION_UPDATED", dict(self.last_cycle))

    def status(self) -> dict:
        m = self.model
        return {"enabled": get_settings().traffic_prediction_enabled, "horizon_min": get_settings().traffic_prediction_horizon_min,
                "model_version": m.version if m else None, "method": m.method if m else None,
                "metrics": m.metrics if m else None, "trained_at": self.trained_at, "last_cycle": self.last_cycle,
                "history_events": self._hist_events}


PREDICTOR = TrafficPredictor()


def predict_cycle() -> None:
    PREDICTOR.predict()


