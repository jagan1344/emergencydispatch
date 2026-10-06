"""Short-horizon traffic prediction: level of a road H simulated minutes from now.

Inputs per road and time t (all derived from the stored traffic_events history + road attributes):
  current level, blocked, active accident, minutes in current state, hour of day (sin/cos), day of week,
  road class, speed limit, the road's historical mean level, the road's change rate.
Two predictors:
  MODEL    RandomForestClassifier trained on (state at t) -> (level at t+H). It is used only when there are
           at least TRAFFIC_MODEL_MIN_SAMPLES samples AND it beats the persistence baseline ("nothing changes")
           on a time-ordered hold-out set (last 25 % of samples).
  FALLBACK transparent rules estimated from the same history (with documented defaults when it is empty):
           * accident / closure: lasts its observed median duration, then the road returns to its usual level;
           * congestion: relaxes toward the road's historical mean level with time constant tau
             (median observed state duration): level(H) = cur + (mean - cur) * (1 - exp(-H / tau)).
Confidence: class probability (MODEL) or the empirical probability that the prediction holds (FALLBACK).
All times are SIMULATED minutes (wall seconds x SIM_TIME_SCALE / 60).
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import RandomForestClassifier

LEVELS = ["FREE", "LIGHT", "MODERATE", "HEAVY", "SEVERE", "BLOCKED"]
HW_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street"]
DEFAULT_INCIDENT_MIN = 30.0   # used only when no accident/closure has ever been observed
DEFAULT_TAU_MIN = 20.0        # used only when no state duration has ever been observed
FEATURES = ["cur", "blocked", "accident", "age_min", "hour_sin", "hour_cos", "dow", "hw", "limit", "hist_mean", "rate"]


@dataclass
class RoadHistory:
    times: list[float] = field(default_factory=list)       # simulated minutes (epoch based)
    levels: list[int] = field(default_factory=list)
    blocked: list[bool] = field(default_factory=list)
    accident: list[bool] = field(default_factory=list)

    def state_at(self, t: float) -> int | None:
        k = bisect.bisect_right(self.times, t) - 1
        return None if k < 0 else k


def hw_index(highway: str) -> int:
    base = highway.replace("_link", "")
    return HW_ORDER.index(base) if base in HW_ORDER else len(HW_ORDER)


def to_sim_min(ts: datetime, scale: float) -> float:
    return ts.timestamp() * scale / 60.0


def build_histories(rows, scale: float) -> dict[str, RoadHistory]:
    """rows: (road_id, created_at, new_level, blocked, event_type) ordered by road, time."""
    out: dict[str, RoadHistory] = defaultdict(RoadHistory)
    for road, ts, lvl, blocked, etype in rows:
        if lvl not in LEVELS:
            continue
        h = out[road]
        h.times.append(to_sim_min(ts, scale))
        h.levels.append(LEVELS.index(lvl))
        h.blocked.append(bool(blocked) or lvl == "BLOCKED")
        prev_acc = h.accident[-1] if h.accident else False
        h.accident.append(etype == "ACCIDENT" or (prev_acc and etype not in ("CLEAR", "UNBLOCK") and lvl != "FREE"))
    return out


def _feat(h: RoadHistory, k: int, t: float, hw: int, limit: float, wall: datetime) -> list[float]:
    prior = h.levels[: k + 1]
    span_h = max(1e-6, (t - h.times[0]) / 60.0)
    hour = wall.hour + wall.minute / 60
    return [h.levels[k], int(h.blocked[k]), int(h.accident[k]), min(240.0, t - h.times[k]),
            math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24), wall.weekday(), hw, limit,
            float(np.mean(prior)), len(prior) / span_h]


@dataclass
class Stats:
    tau_min: float = DEFAULT_TAU_MIN
    incident_min: float = DEFAULT_INCIDENT_MIN
    p_persist: float = 0.5
    road_mean: dict[str, float] = field(default_factory=dict)
    observed_durations: int = 0
    observed_incidents: int = 0


def estimate_stats(hist: dict[str, RoadHistory], horizon: float) -> Stats:
    st = Stats()
    durs, inc_durs, keep = [], [], []
    for road, h in hist.items():
        st.road_mean[road] = float(np.mean(h.levels))
        for k in range(len(h.times) - 1):
            d = h.times[k + 1] - h.times[k]
            durs.append(d)
            if h.accident[k] or h.blocked[k]:
                inc_durs.append(d)
            keep.append(d >= horizon)
    if durs:
        st.tau_min, st.observed_durations = float(np.median(durs)), len(durs)
    if inc_durs:
        st.incident_min, st.observed_incidents = float(np.median(inc_durs)), len(inc_durs)
    if len(keep) >= 30:
        st.p_persist = float(np.mean(keep))
    return st


def fallback_predict(cur: int, blocked: bool, accident: bool, age_min: float, road_mean: float | None,
                     horizon: float, st: Stats) -> tuple[int, float]:
    """Transparent rule model -> (predicted level index, confidence)."""
    if blocked or accident:
        remaining = st.incident_min - age_min
        if remaining > horizon:
            return cur, st.p_persist
        target = road_mean if road_mean is not None else LEVELS.index("MODERATE")
        return int(round(min(target, 4))), 1 - st.p_persist
    target = road_mean if road_mean is not None else 0.0
    nxt = cur + (target - cur) * (1 - math.exp(-horizon / max(st.tau_min, 1e-3)))
    pred = int(round(min(4, max(0, nxt))))
    return pred, (st.p_persist if pred == cur else 1 - st.p_persist)


@dataclass
class TrafficModel:
    horizon_min: float
    version: str
    method: str                       # MODEL / FALLBACK
    stats: Stats
    clf: RandomForestClassifier | None = None
    metrics: dict = field(default_factory=dict)

    def predict(self, rows: list[dict]) -> list[tuple[int, float, str]]:
        """rows: dicts with keys cur, blocked, accident, age_min, road_id, hw, limit, wall(datetime), rate."""
        out = []
        X = []
        for r in rows:
            mean = self.stats.road_mean.get(r["road_id"])
            hour = r["wall"].hour + r["wall"].minute / 60
            X.append([r["cur"], int(r["blocked"]), int(r["accident"]), min(240.0, r["age_min"]),
                      math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24), r["wall"].weekday(),
                      r["hw"], r["limit"], mean if mean is not None else float(r["cur"]), r.get("rate", 0.0)])
        if self.clf is not None and rows:
            proba = self.clf.predict_proba(np.array(X))
            classes = list(self.clf.classes_)
            for p in proba:
                k = int(np.argmax(p))
                out.append((int(classes[k]), float(p[k]), "MODEL"))
            return out
        for r in rows:
            lvl, conf = fallback_predict(r["cur"], r["blocked"], r["accident"], r["age_min"],
                                         self.stats.road_mean.get(r["road_id"]), self.horizon_min, self.stats)
            out.append((lvl, round(conf, 3), "FALLBACK"))
        return out


def train(hist: dict[str, RoadHistory], roads: dict[str, tuple[str, float]], horizon_min: float, scale: float,
          now_sim: float, min_samples: int, max_samples: int = 30_000, seed: int = 0) -> TrafficModel:
    """roads: road_id -> (highway_type, speed_limit). Returns MODEL if it beats persistence, else FALLBACK."""
    stats = estimate_stats(hist, horizon_min)
    version_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    X, y = [], []
    times = []
    rng = np.random.default_rng(seed)
    for road, h in hist.items():
        hw, limit = roads.get(road, ("unclassified", 30.0))
        hwi = hw_index(hw)
        end = now_sim - horizon_min
        if not h.times or end <= h.times[0]:
            continue
        grid = np.arange(h.times[0], end, max(horizon_min / 2, 0.5))[:200]
        pts = sorted(set(list(grid) + [t for t in h.times if t <= end]))
        for t in pts:
            k = h.state_at(t)
            k2 = h.state_at(t + horizon_min)
            if k is None or k2 is None:
                continue
            wall = datetime.fromtimestamp(t * 60 / scale, tz=timezone.utc)
            X.append(_feat(h, k, t, hwi, limit, wall))
            y.append(h.levels[k2])
            times.append(t)
    n = len(y)
    if n > max_samples:
        idx = np.sort(rng.choice(n, max_samples, replace=False))
        X, y, times = [X[i] for i in idx], [y[i] for i in idx], [times[i] for i in idx]
        n = max_samples
    metrics: dict = {"samples": n, "roads_with_history": len(hist), "min_samples": min_samples,
                     "tau_min": round(stats.tau_min, 2), "incident_duration_min": round(stats.incident_min, 2),
                     "p_persist": round(stats.p_persist, 3), "observed_state_durations": stats.observed_durations}
    fallback = TrafficModel(horizon_min, f"traffic-fallback-v1-{version_ts}", "FALLBACK", stats, None, metrics)
    if n < min_samples or len(set(y)) < 2:
        metrics["reason"] = f"insufficient history ({n} samples < {min_samples}) - transparent fallback model"
        return fallback
    order = np.argsort(times)
    X, y = np.array(X)[order], np.array(y)[order]
    cut = int(n * 0.75)
    clf = RandomForestClassifier(n_estimators=120, max_depth=12, min_samples_leaf=3, random_state=seed, n_jobs=1)
    clf.fit(X[:cut], y[:cut])
    pred = clf.predict(X[cut:])
    acc_model = float(np.mean(pred == y[cut:]))
    acc_persist = float(np.mean(X[cut:, 0] == y[cut:]))
    mae_model = float(np.mean(np.abs(pred - y[cut:])))
    mae_persist = float(np.mean(np.abs(X[cut:, 0] - y[cut:])))
    metrics.update({"holdout": int(n - cut), "accuracy_model": round(acc_model, 4), "accuracy_persistence": round(acc_persist, 4),
                    "mae_model": round(mae_model, 4), "mae_persistence": round(mae_persist, 4)})
    if acc_model + 1e-9 < acc_persist + 0.01 and mae_model >= mae_persist:
        metrics["reason"] = "RandomForest did not beat the persistence baseline on the hold-out - fallback model used"
        fallback.metrics = metrics
        return fallback
    clf.fit(X, y)       # final model on all samples
    metrics["reason"] = "RandomForest beats the persistence baseline on the time-ordered hold-out"
    return TrafficModel(horizon_min, f"traffic-rf-{version_ts}", "MODEL", stats, clf, metrics)
