"""Experiment orchestration: generate the scenarios ONCE, replay each of them under every requested strategy,
persist every result (a failing scenario/strategy is recorded and does not stop the run), then aggregate.

Reuses the already-loaded runtime when it runs inside the backend (road graph, severity model) and loads the
same objects itself when started from the command line. The live graph is never modified: each simulation runs
on RoadGraph.clone().
"""
from __future__ import annotations

import logging
import math
import platform
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import PROJECT_DIR, get_settings
from app.dispatch.triage import triage
from app.evaluation import metrics as M
from app.evaluation import statistics as S
from app.evaluation.scenarios import ScenarioOptions, ScenarioSpace, generate_scenarios, summary as scenario_summary
from app.evaluation.simulator import SimParams, Simulation
from app.evaluation.strategies import ABLATIONS, FULL, PROGRESSIVE, StrategyConfig
from app.utils.logging import log_event

log = logging.getLogger("app.evaluation")
RESULTS_DIR = PROJECT_DIR / "evaluation" / "results"


@dataclass
class EvalContext:
    settings: object
    base_graph: object
    osrm: object | None
    severity_model: object
    traffic_model: object | None
    space: ScenarioSpace
    info: dict = field(default_factory=dict)


def _git() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=PROJECT_DIR, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:  # git not installed / not a checkout
            return None
    sha = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=no")
    return {"git_commit": sha or "unknown", "git_branch": run("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "git_dirty": None if status is None else bool(status)}


def load_hospitals(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, latitude, longitude, emergency_capacity, icu_available, trauma_available, "
            "cardiac_available, stroke_available FROM hospitals WHERE status <> 'CLOSED' ORDER BY id")).all()
    return [{"id": r[0], "name": r[1], "lat": float(r[2]), "lon": float(r[3]), "emergency_capacity": int(r[4]),
             "icu_capacity": int(r[5] or 0), "trauma": bool(r[6]), "cardiac": bool(r[7]), "stroke": bool(r[8])} for r in rows]


def build_context(use_osrm: bool | None = None, traffic_model: str = "trained") -> EvalContext:
    """traffic_model: 'trained' = same procedure as the live predictor (RandomForest if it beats persistence on the
    stored history, else the transparent FALLBACK); 'fallback' = force the fallback."""
    from app.database import get_engine
    from app.ml.predict import SeverityModel
    from app.routing.graph import RoadGraph
    from app.routing.osrm_client import OsrmClient
    from app.services.state import STATE
    from app.services.traffic_prediction import TrafficPredictor

    st = get_settings()
    engine = get_engine()
    graph = STATE.graph if STATE.graph is not None else RoadGraph.load(engine)
    model = STATE.model if getattr(STATE.model, "available", False) else SeverityModel()
    if not model.available:
        model.load()
    osrm, osrm_note = None, "disabled"
    if use_osrm is not False and st.osrm_url:
        client = OsrmClient(st.osrm_url, st.osrm_timeout_s)
        try:
            client.route(float(graph.lat[0]), float(graph.lon[0]), float(graph.lat[-1]), float(graph.lon[-1]), alternatives=1)
            osrm, osrm_note = client, f"enabled ({st.osrm_url})"
        except Exception as exc:  # OSRM not running: graph-only routing, recorded in the metadata
            if use_osrm:
                raise RuntimeError(f"OSRM requested but unreachable at {st.osrm_url}: {exc}") from exc
            osrm_note = f"unreachable at {st.osrm_url} - graph-only routing"
    tm = None
    tm_info: dict = {"method": "DISABLED"}
    if st.traffic_prediction_enabled:
        pred = TrafficPredictor()                 # separate instance: the live predictor is not touched
        tm = pred.retrain()
        if traffic_model == "fallback" and tm.method != "FALLBACK":
            from app.ml.traffic_model import TrafficModel
            tm = TrafficModel(tm.horizon_min, tm.version.replace("rf", "fallback-forced"), "FALLBACK", tm.stats, None,
                              dict(tm.metrics, reason="fallback forced for this experiment"))
        tm_info = {"version": tm.version, "method": tm.method,
                   "label": "LEARNED" if tm.method == "MODEL" else "FALLBACK", "metrics": tm.metrics}
    space = ScenarioSpace(graph, st.city_lat, st.city_lon, st.city_radius_m, load_hospitals(engine))
    info = {
        **_git(), "city": {"name": st.city_name, "lat": st.city_lat, "lon": st.city_lon, "radius_m": st.city_radius_m},
        "road_network": space.fingerprint(), "routing": {"graph_source": graph.source, "osrm": osrm_note},
        "severity_model": {"version": model.version, "dataset": model.dataset, "available": model.available},
        "traffic_model": tm_info,
        "hospital_model": {"version": "hospital-queue-v1", "label": "ESTIMATE (queueing approximation)"},
        "hospital_templates": space.hospitals,
        "settings": {k: getattr(st, k) for k in (
            "dispatch_confidence_high", "dispatch_confidence_low", "human_review_timeout_s",
            "traffic_prediction_horizon_min", "hospital_prediction_horizon_min", "hospital_rate_window_min",
            "ed_mean_stay_min", "ed_treatment_slot_min", "hospital_max_wait_min",
            "resource_reallocation_threshold_s", "reallocation_min_gain_s", "reallocation_max_donor_delay_s",
            "reallocation_priority_margin", "reroute_threshold", "reroute_min_eta_savings_s",
            "reroute_min_improvement_percent", "reroute_cooldown_s", "reroute_oscillation_similarity",
            "max_dispatch_candidates", "scene_time_s", "handover_time_s")},
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
    }
    return EvalContext(st, graph, osrm, model, tm, space, info)


def default_strategies(progressive: bool, ablation: bool) -> list[StrategyConfig]:
    out: list[StrategyConfig] = []
    if progressive:
        out += list(PROGRESSIVE.values())
    if ablation:
        if FULL not in out:
            out.append(FULL)
        out += list(ABLATIONS.values())
    return out


def run_scenario(ctx: EvalContext, sc: dict, strategy: StrategyConfig, triages: dict, params: SimParams) -> dict:
    t0 = time.perf_counter()
    sim = Simulation(ctx, sc, strategy, triages, params).run()
    records = M.incident_records(sim, strategy.name)
    return {"status": "OK", "metrics": M.scenario_metrics(sim, records), "incidents": records,
            "conflicts": sim.conflicts, "runtime_ms": (time.perf_counter() - t0) * 1000}


def _kind(strategies: list[StrategyConfig]) -> str:
    names = {s.name for s in strategies}
    if names == set(PROGRESSIVE):
        return "PROGRESSIVE"
    if names == {FULL.name, *ABLATIONS}:
        return "ABLATION"
    if set(PROGRESSIVE) | set(ABLATIONS) == names:
        return "MIXED"
    return "CUSTOM"


def run_experiment(ctx: EvalContext, strategies: list[StrategyConfig], n_scenarios: int, seed: int, *,
                   run_name: str | None = None, params: SimParams | None = None, options: ScenarioOptions | None = None,
                   persist: bool = True, write_files: bool = True, progress=None) -> dict:
    if n_scenarios < 1:
        raise ValueError("at least one scenario is required")
    if not strategies:
        raise ValueError("no strategy selected")
    params = params or SimParams()
    options = options or ScenarioOptions(scene_time_s=ctx.settings.scene_time_s,
                                         handover_time_s=ctx.settings.handover_time_s,
                                         ed_mean_stay_min=ctx.settings.ed_mean_stay_min)
    started = datetime.now(timezone.utc)
    run_name = run_name or f"{_kind(strategies).lower()}-s{seed}-n{n_scenarios}-{started:%Y%m%d-%H%M%S}"
    config = {"strategies": {s.name: s.flags() for s in strategies}, "simulation": asdict(params),
              "scenario_options": options.as_dict()}
    meta = {**ctx.info, "started_at": started.isoformat(), "seed": seed, "scenario_count": n_scenarios,
            "strategies": [s.name for s in strategies], "kind": _kind(strategies)}
    store = None
    if persist:
        from app.evaluation.store import ExperimentStore
        store = ExperimentStore.create(run_name, _kind(strategies), [s.name for s in strategies], config,
                                       n_scenarios, seed, meta)
    log_event(log, "EXPERIMENT_STARTED", run=run_name, strategies=[s.name for s in strategies], scenarios=n_scenarios,
              seed=seed, traffic_model=ctx.info["traffic_model"].get("label"))
    results: dict[str, dict[int, dict]] = {s.name: {} for s in strategies}
    incidents: list[dict] = []
    failures: list[dict] = []
    scenarios = generate_scenarios(ctx.space, seed, n_scenarios, options)
    total = len(scenarios) * len(strategies)
    done = 0
    try:
        for sc in scenarios:
            sc_id = store.add_scenario(sc, scenario_summary(sc)) if store else None
            st = ctx.settings
            triages = {i["id"]: triage(i["case"], ctx.severity_model, st.dispatch_confidence_high,
                                       st.dispatch_confidence_low) for i in sc["incidents"]}
            for strat in strategies:
                try:
                    res = run_scenario(ctx, sc, strat, triages, params)
                except Exception as exc:   # recorded, the run continues with the next scenario/strategy
                    res = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                           "metrics": None, "incidents": [], "runtime_ms": None}
                    failures.append({"scenario": sc["number"], "strategy": strat.name, "error": res["error"][:400]})
                    log_event(log, "EXPERIMENT_SCENARIO_FAILED", run=run_name, scenario=sc["number"], strategy=strat.name,
                              error=f"{type(exc).__name__}: {exc}")
                if res["status"] == "OK":
                    results[strat.name][sc["number"]] = res["metrics"]
                    incidents += res["incidents"]
                if store:
                    store.add_result(sc_id, strat.name, res)
                done += 1
                if progress:
                    progress(done, total)
        summary = aggregate(results, strategies, failures, meta)
        status = "COMPLETED" if not failures else ("PARTIAL" if any(results.values()) else "FAILED")
        summary["status"] = status
        out_dir = None
        if write_files:
            from app.evaluation.report import write_report
            out_dir = write_report(run_name, summary, results, incidents, scenarios, meta, config)
        meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        if store:
            store.finish(status, summary, meta, str(out_dir) if out_dir else None)
        log_event(log, "EXPERIMENT_COMPLETED", run=run_name, status=status, failures=len(failures),
                  output=str(out_dir) if out_dir else None)
        return {"run_name": run_name, "status": status, "output_dir": str(out_dir) if out_dir else None,
                "summary": summary, "results": results, "incidents": incidents, "scenarios": scenarios,
                "run_id": str(store.run_id) if store else None}
    except Exception as exc:
        if store:
            store.fail(f"{type(exc).__name__}: {exc}")
        raise


def aggregate(results: dict[str, dict[int, dict]], strategies: list[StrategyConfig], failures: list[dict],
              meta: dict) -> dict:
    names = [s.name for s in strategies]
    metric_keys = list(M.METRICS)
    descriptive = {n: {m: S.describe([v.get(m) for v in results[n].values()]) for m in metric_keys} for n in names}
    comparison = [{"metric": m, "label": M.METRICS[m][0], "unit": M.METRICS[m][1], "direction": M.METRICS[m][2],
                   "values": {n: descriptive[n][m]["mean"] for n in names},
                   "n": {n: descriptive[n][m]["n"] for n in names}} for m in metric_keys]
    out = {"strategies": names, "comparison": comparison, "descriptive": descriptive, "failures": failures,
           "traffic_model": meta.get("traffic_model"), "seed": meta.get("seed"), "scenario_count": meta.get("scenario_count")}
    prog = [n for n in PROGRESSIVE if n in names]
    if "BASELINE" in names and len(prog) > 1:
        out["paired_vs_baseline"] = S.compare({n: results[n] for n in prog}, "BASELINE", metric_keys)
        inc = []
        for a, b in zip(prog, prog[1:]):                    # incremental contribution of each added capability
            inc += S.compare({a: results[a], b: results[b]}, a, metric_keys)
        out["incremental"] = inc
    abl = [n for n in names if n in ABLATIONS]
    if FULL.name in names and abl:
        out["ablation_vs_full"] = S.compare({n: results[n] for n in [FULL.name, *abl]}, FULL.name, metric_keys)
    out["utilization_by_unit"] = {n: _mean_util(results[n]) for n in names}
    return out


def _mean_util(rows: dict[int, dict]) -> float | None:
    vals = [v["ambulance_utilization"] for v in rows.values() if v.get("ambulance_utilization") is not None]
    return sum(vals) / len(vals) if vals else None


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if hasattr(x, "item") and type(x).__module__ == "numpy":
        return x.item()
    return x
