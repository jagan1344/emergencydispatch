"""Research evaluation layer: strategies, reproducibility, metrics, statistics, runner, persistence and API.

All tests run on the synthetic test network (no internet, no OSRM, no MQTT)."""
from __future__ import annotations

import math

import numpy as np
import pytest
from sqlalchemy import text

from app.evaluation import statistics as S
from app.evaluation.metrics import _busy_time, _no_unit_share, incident_records, scenario_metrics
from app.evaluation.scenarios import ScenarioOptions, generate_scenario, generate_scenarios
from app.evaluation.simulator import Simulation, Unit
from app.evaluation.strategies import (ABLATION_CAPABILITIES, ABLATIONS, BASELINE, CAPABILITIES, FULL, HOSPITAL,
                                       PROGRESSIVE, SEVERITY, TRAFFIC, resolve)


@pytest.fixture(scope="module")
def ctx(database):
    from app.evaluation.runner import build_context
    return build_context(use_osrm=False)


def _triages(ctx, sc):
    from app.dispatch.triage import triage
    st = ctx.settings
    return {i["id"]: triage(i["case"], ctx.severity_model, st.dispatch_confidence_high, st.dispatch_confidence_low)
            for i in sc["incidents"]}


def _nodes_by_distance(ctx, center: int) -> list[tuple[float, int]]:
    """(free-flow road distance in m, node) from every service-area node to `center`."""
    g = ctx.base_graph
    out = []
    for n in ctx.space.nodes:
        path, _ = g.shortest_path(int(n), center, "length")
        if path and int(n) != center:
            out.append((sum(float(g.e_len[e]) for e in g.path_edges(path)), int(n)))
    return sorted(out)


def _point(ctx, n):
    return float(ctx.base_graph.lat[n]), float(ctx.base_graph.lon[n])


CRITICAL_CASE = {"emergency_type": "accident", "patient_age": 40, "heart_rate": 150, "respiratory_rate": 34,
                 "oxygen_saturation": 82, "consciousness": "UNRESPONSIVE", "bleeding": "SEVERE",
                 "injury_severity": "SEVERE", "accident_type": "ROAD", "breathing_difficulty": True, "chest_pain": False}
MILD_CASE = {"emergency_type": "other", "patient_age": 30, "heart_rate": 80, "respiratory_rate": 15,
             "oxygen_saturation": 98, "consciousness": "ALERT", "bleeding": "NONE", "injury_severity": "MINOR",
             "accident_type": "NONE", "breathing_difficulty": False, "chest_pain": False}


def _scenario(ctx, fleet, incidents, hospitals=None, traffic=None, events=None):
    hs = hospitals if hospitals is not None else [
        {**h, "icu_available": 2, "initial_discharges_s": [], "background": []} for h in ctx.space.hospitals]
    return {"number": 1, "seed": 0, "start_hour": 10, "duration_s": 1800.0, "traffic_regime": "TEST",
            "hospital_load_regime": "TEST", "fleet": fleet, "incidents": incidents, "hospitals": hs,
            "initial_traffic": traffic or {}, "events": events or []}


def _unit(ctx, uid, node, equipment):
    lat, lon = _point(ctx, node)
    return {"id": uid, "node": node, "lat": lat, "lon": lon, "equipment_level": equipment, "fuel_level": 90.0,
            "missions_today": 0, "committed_until_s": None}


def _incident(ctx, node, case, true_sev, iid="T-I01", t=0.0):
    lat, lon = _point(ctx, node)
    return {"id": iid, "t": t, "node": node, "lat": lat, "lon": lon, "case": case, "true_case": case,
            "report_quality": "CLEAR", "true_severity": true_sev, "scene_time_s": 60.0, "handover_time_s": 60.0,
            "hospital_stay_s": 3600.0, "review_delay_s": 30.0}


def _first_dispatch(ctx, sc, strategy):
    sim = Simulation(ctx, sc, strategy, _triages(ctx, sc))
    sim._apply_events()
    sim._dispatcher()
    return sim, sim.incs[0]


# ------------------------------------------------------------------------------------------ strategies
def test_strategy_definitions_are_progressive_and_ablations_remove_one_capability():
    order = [PROGRESSIVE[n] for n in ("BASELINE", "SEVERITY", "TRAFFIC", "HOSPITAL", "FULL")]
    for a, b in zip(order, order[1:]):           # every system keeps all capabilities of the previous one
        assert all(getattr(b, c) for c in CAPABILITIES if getattr(a, c))
        assert sum(map(bool, b.flags().values())) > sum(map(bool, a.flags().values()))
    assert not any(BASELINE.flags().values()) and all(FULL.flags().values())
    assert len(ABLATIONS) == len(ABLATION_CAPABILITIES) == 6
    for cap in ABLATION_CAPABILITIES:
        abl = FULL.without(cap)
        assert [c for c in CAPABILITIES if getattr(abl, c) != getattr(FULL, c)] == [cap]
    assert [s.name for s in resolve(["baseline", "FULL-TRAFFIC"])] == ["BASELINE", "FULL-TRAFFIC"]
    with pytest.raises(ValueError):
        resolve(["NOPE"])


def test_baseline_dispatches_nearest_unit_severity_dispatches_capable_unit(ctx):
    center = int(ctx.space.nodes[len(ctx.space.nodes) // 2])
    dist = _nodes_by_distance(ctx, center)
    near, far = dist[1][1], dist[len(dist) // 3][1]
    sc = _scenario(ctx, [_unit(ctx, "U-NEAR-BASIC", near, "BASIC"), _unit(ctx, "U-FAR-ICU", far, "ICU")],
                   [_incident(ctx, center, CRITICAL_CASE, "CRITICAL")])
    _, inc = _first_dispatch(ctx, sc, BASELINE)
    assert inc.unit.id == "U-NEAR-BASIC"                    # conventional: nearest available unit, no triage
    assert inc.severity is None and inc.required == "BASIC"
    _, inc = _first_dispatch(ctx, sc, SEVERITY)
    assert inc.severity == "CRITICAL" and inc.required == "ICU"
    assert inc.unit.id == "U-FAR-ICU"                       # severity-aware: the unit that can treat the patient


def test_traffic_strategy_avoids_congested_unit(ctx):
    from app.routing.engine import RoutingEngine, RoutingPolicy
    g = ctx.base_graph.clone()
    g.reset_traffic()
    eng = RoutingEngine(g, None, RoutingPolicy(traffic_aware=False))
    center = int(ctx.space.nodes[len(ctx.space.nodes) // 2])
    c_pt = _point(ctx, center)
    near = _nodes_by_distance(ctx, center)[1][1]
    near_rr = eng.route(_point(ctx, near), c_pt)
    jam = sorted(near_rr.road_ids())                       # congest the nearer unit's whole approach
    jammed_time = near_rr.base_duration_s / 0.3            # SEVERE congestion factor 0.30
    # a unit that is farther (static view) but whose approach avoids the jam and is faster under it
    far = None
    for _, n in _nodes_by_distance(ctx, center)[2:]:
        rr = eng.route(_point(ctx, n), c_pt)
        if not (rr.road_ids() & set(jam)) and near_rr.base_duration_s * 1.1 < rr.base_duration_s < jammed_time * 0.8:
            far = n
            break
    assert far is not None, "test network has no suitable second unit position"
    sc = _scenario(ctx, [_unit(ctx, "U-NEAR", near, "ICU"), _unit(ctx, "U-FAR", far, "ICU")],
                   [_incident(ctx, center, MILD_CASE, "LOW")], traffic={"SEVERE": jam})
    _, inc = _first_dispatch(ctx, sc, SEVERITY)
    assert inc.unit.id == "U-NEAR"                          # static routing ignores congestion
    sim_t, inc_t = _first_dispatch(ctx, sc, TRAFFIC)
    etas = {c["unit"].id: c["eta"] for c in sim_t._candidates(inc_t, list(sim_t.units.values()), 8)}
    assert etas["U-FAR"] < etas["U-NEAR"]                   # the jam really makes U-NEAR slower
    assert inc_t.unit.id == "U-FAR"                         # traffic-aware DispatchScore picks the faster unit


def test_hospital_strategy_uses_congestion_prediction(ctx):
    hs = sorted(ctx.space.hospitals, key=lambda h: h["id"])[:2]
    assert len(hs) == 2
    patient = min(ctx.space.nodes, key=lambda n: (ctx.base_graph.lat[n] - hs[0]["lat"]) ** 2 + (ctx.base_graph.lon[n] - hs[0]["lon"]) ** 2)
    common = {"icu_available": 0, "trauma": False, "cardiac": False, "stroke": False, "background": []}
    full_cap = 10
    busy = {**hs[0], **common, "emergency_capacity": full_cap, "initial_discharges_s": [9e6] * 10}   # at capacity
    free = {**hs[1], **common, "emergency_capacity": full_cap, "initial_discharges_s": []}
    sc = _scenario(ctx, [_unit(ctx, "U1", int(patient), "BASIC")], [_incident(ctx, int(patient), MILD_CASE, "LOW")],
                   hospitals=[busy, free])
    chosen = {}
    for strat in (TRAFFIC, HOSPITAL):
        sim = Simulation(ctx, sc, strat, _triages(ctx, sc))
        sim._apply_events()
        sim._dispatcher()
        u = sim.units["U1"]
        sim._move(5000)                     # drive to the (nearby) patient
        sim.t = u.timer_until or sim.t
        sim._timers()                       # on-scene time over -> hospital selection
        chosen[strat.name] = sim.incs[0]
    assert chosen["TRAFFIC"].hospital_id == hs[0]["id"]          # nearest / fastest hospital
    assert chosen["TRAFFIC"].hospital_wait_pred_s is None        # no prediction used
    assert chosen["HOSPITAL"].hospital_id == hs[1]["id"]         # predicted congestion avoided
    assert chosen["HOSPITAL"].hospital_wait_pred_s is not None


def test_full_strategy_enables_every_capability(ctx):
    sc = generate_scenario(ctx.space, 42, 3, ScenarioOptions())
    sim = Simulation(ctx, sc, FULL, _triages(ctx, sc)).run()
    m = scenario_metrics(sim, incident_records(sim, "FULL"))
    assert m["served_calls"] == m["calls"] and m["explanations"] > 0
    assert m["route_checks"] > 0 and m["traffic_predictions"] > 0
    base = Simulation(ctx, sc, BASELINE, _triages(ctx, sc)).run()
    mb = scenario_metrics(base, incident_records(base, "BASELINE"))
    assert mb["route_checks"] == 0 and mb["traffic_predictions"] == 0 and mb["explanations"] == 0
    # explainability never changes a decision
    no_x = Simulation(ctx, sc, FULL.without("explainability"), _triages(ctx, sc)).run()
    assert [i.arrival_t for i in no_x.incs] == [i.arrival_t for i in sim.incs]


def test_live_graph_is_never_modified(ctx):
    from app.services.state import STATE
    before = (STATE.graph.version, list(STATE.graph.road_level), STATE.graph.road_blocked.copy())
    sc = generate_scenario(ctx.space, 5, 1, ScenarioOptions())
    Simulation(ctx, sc, FULL, _triages(ctx, sc)).run()
    assert STATE.graph.version == before[0] and STATE.graph.road_level == before[1]
    assert np.array_equal(STATE.graph.road_blocked, before[2])


# ------------------------------------------------------------------------------------------ reproducibility
def test_same_seed_same_scenarios_and_results(ctx):
    a = generate_scenarios(ctx.space, 42, 3)
    b = generate_scenarios(ctx.space, 42, 3)
    assert [s["fingerprint"] for s in a] == [s["fingerprint"] for s in b]
    assert a == b
    # scenario k does not depend on how many scenarios are generated
    assert generate_scenarios(ctx.space, 42, 5)[1]["fingerprint"] == a[1]["fingerprint"]
    assert generate_scenarios(ctx.space, 43, 3)[0]["fingerprint"] != a[0]["fingerprint"]
    # the simulation is deterministic
    r1 = Simulation(ctx, a[0], FULL, _triages(ctx, a[0])).run()
    r2 = Simulation(ctx, b[0], FULL, _triages(ctx, b[0])).run()
    assert [i.arrival_t for i in r1.incs] == [i.arrival_t for i in r2.incs]
    assert [i.hospital_id for i in r1.incs] == [i.hospital_id for i in r2.incs]


def test_scenarios_cover_the_required_situations(ctx):
    scs = generate_scenarios(ctx.space, 42, 40)
    sev = {i["true_severity"] for s in scs for i in s["incidents"]}
    assert sev == {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert {s["traffic_regime"] for s in scs} == {"FREE", "LIGHT", "MODERATE", "SEVERE"}
    assert {s["hospital_load_regime"] for s in scs} == {"LOW", "MEDIUM", "HIGH"}
    kinds = {e["kind"] for s in scs for e in s["events"]}
    assert {"BLOCK", "ACCIDENT", "CONGESTION", "CLEAR", "AMBULANCE_UNAVAILABLE", "HOSPITAL_SURGE"} <= kinds
    assert any(u["committed_until_s"] for s in scs for u in s["fleet"])
    assert {u["equipment_level"] for s in scs for u in s["fleet"]} == {"BASIC", "ADVANCED", "ICU"}
    assert {i["report_quality"] for s in scs for i in s["incidents"]} == {"CLEAR", "UNCERTAIN"}


# ------------------------------------------------------------------------------------------ metrics & statistics
def test_utilization_and_availability_metrics():
    tl = [(0.0, "AVAILABLE"), (100.0, "TO_PATIENT"), (300.0, "HANDOVER"), (400.0, "AVAILABLE"), (900.0, "OFFLINE")]
    busy, avail = _busy_time(tl, 1000.0)
    assert busy == pytest.approx(300.0) and avail == pytest.approx(100.0 + 500.0)
    u1 = Unit("A", "BASIC", 90, 0, 0, 0, timeline=[(0.0, "AVAILABLE"), (100.0, "TO_PATIENT"), (600.0, "AVAILABLE")])
    u2 = Unit("B", "BASIC", 90, 0, 0, 0, timeline=[(0.0, "COMMITTED"), (300.0, "AVAILABLE")])
    assert _no_unit_share([u1, u2], 1000.0) == pytest.approx(0.2)        # 100..300 s nobody available


def test_incident_metric_definitions(ctx):
    sc = generate_scenario(ctx.space, 42, 2, ScenarioOptions())
    sim = Simulation(ctx, sc, HOSPITAL, _triages(ctx, sc)).run()
    for r, inc in zip(incident_records(sim, "HOSPITAL"), sim.incs):
        c = inc.spec["t"]
        assert r["response_time_s"] == pytest.approx(inc.arrival_t - c, abs=0.06)
        assert r["patient_wait_s"] == pytest.approx(r["response_time_s"] + inc.spec["scene_time_s"], abs=0.11)
        assert r["dispatch_delay_s"] <= r["response_time_s"]
        if inc.spec["true_severity"] != "CRITICAL":
            assert r["critical_delay_s"] is None


def test_paired_statistics():
    rng = np.random.default_rng(1)
    ref = {k: float(v) for k, v in enumerate(rng.normal(600, 60, 30))}
    better = {k: v - 40 + float(rng.normal(0, 10)) for k, v in ref.items()}
    r = S.paired(ref, better, "response_time_s")
    assert r["pairs"] == 30 and r["test"] == "paired t-test" and r["p_value"] < 0.001
    assert r["mean_difference"] == pytest.approx(-40, abs=6) and r["improvement"] > 0      # lower response = better
    assert r["ci95_low"] < r["mean_difference"] < r["ci95_high"]
    skewed = {k: v - float(rng.exponential(30)) for k, v in ref.items()}
    for k in range(5):
        skewed[k] = ref[k] - 400.0                       # heavy outliers -> non-normal differences
    assert S.paired(ref, skewed, "response_time_s")["test"] == "Wilcoxon signed-rank"
    small = S.paired({0: 1.0, 1: 2.0}, {0: 2.0, 1: 3.0}, "response_time_s")
    assert small["test"] is None and "insufficient" in small["note"]
    same = S.paired(ref, dict(ref), "response_time_s")
    assert same["test"] == "none" and same["mean_difference"] == 0
    assert S.paired({0: None, 1: 5.0}, {0: 1.0, 1: None}, "x")["pairs"] == 0
    rows = [{"p_value": 0.01}, {"p_value": 0.04}, {"p_value": 0.03}, {"p_value": None}]
    S.holm(rows)
    assert [r["p_adjusted"] for r in rows[:3]] == pytest.approx([0.03, 0.06, 0.06])
    assert rows[0]["significant"] and not rows[1]["significant"] and rows[3]["p_adjusted"] is None
    d = S.describe([1.0, 2.0, 3.0, 4.0, None])
    assert d["n"] == 4 and d["median"] == 2.5 and d["p25"] == 1.75 and d["min"] == 1.0


# ------------------------------------------------------------------------------------------ runner & persistence
def _operational_counts():
    from app.database import get_engine
    with get_engine().connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in
                ("emergency_incidents", "dispatches", "routes", "ambulances", "hospitals", "resource_conflicts",
                 "traffic_events", "system_events")}


def test_runner_persists_results_and_isolates_failures(ctx, monkeypatch, tmp_path):
    from app.evaluation import report, runner
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    orig = runner.run_scenario

    def flaky(c, sc, strategy, triages, params):
        if strategy.name == "SEVERITY" and sc["number"] == 2:
            raise RuntimeError("injected failure")
        return orig(c, sc, strategy, triages, params)

    monkeypatch.setattr(runner, "run_scenario", flaky)
    before = _operational_counts()
    res = runner.run_experiment(ctx, [BASELINE, SEVERITY, FULL], 3, 11, run_name="pytest-eval")
    assert _operational_counts() == before                     # operational tables untouched
    assert res["status"] == "PARTIAL"
    s = res["summary"]
    assert s["failures"] == [{"scenario": 2, "strategy": "SEVERITY", "error": s["failures"][0]["error"]}]
    assert "injected failure" in s["failures"][0]["error"]
    assert set(res["results"]["BASELINE"]) == {1, 2, 3} and set(res["results"]["SEVERITY"]) == {1, 3}
    from app.database import get_engine
    with get_engine().connect() as c:
        run = c.execute(text("SELECT status, scenario_count, random_seed, metadata->>'git_commit', "
                             "metadata->'traffic_model'->>'label' FROM experiment_runs WHERE run_name='pytest-eval'")).one()
        rows = c.execute(text("SELECT r.strategy, r.status, count(*) FROM experiment_results r JOIN experiment_runs e "
                              "ON e.id = r.experiment_run_id WHERE e.run_name='pytest-eval' GROUP BY 1, 2")).all()
        fp = c.execute(text("SELECT count(DISTINCT s.fingerprint) FROM experiment_scenarios s JOIN experiment_runs e "
                            "ON e.id = s.experiment_run_id WHERE e.run_name='pytest-eval'")).scalar()
    assert run[0] == "PARTIAL" and run[1] == 3 and run[2] == 11 and run[3] and run[4] in ("LEARNED", "FALLBACK")
    assert dict(((a, b), n) for a, b, n in rows) == {("BASELINE", "OK"): 3, ("SEVERITY", "OK"): 2,
                                                     ("SEVERITY", "FAILED"): 1, ("FULL", "OK"): 3}
    assert fp == 3
    out = tmp_path / "pytest-eval"
    report = (out / "REPORT.md").read_text()
    assert "| **Simulation runs** | **9** | 3 scenarios × 3 configurations" in report       # denominators stated
    assert f"| Failed runs | 1 |" in report and "It is NOT a mean over 9 runs" in report
    assert f"| Emergency calls per configuration | {sum(len(s['incidents']) for s in res['scenarios'])} |" in report
    from app.evaluation.report import refresh_design_section
    refresh_design_section(out)                                  # idempotent: one section after a refresh
    assert (out / "REPORT.md").read_text().count("## Design and denominators") == 1
    for f in ("comparison_summary.csv", "scenario_results.csv", "incident_results.csv", "statistics.csv",
              "summary.json", "run_metadata.json", "REPORT.md", "failures.csv"):
        assert (out / f).exists(), f
    assert (out / "plots").is_dir() and any((out / "plots").glob("progressive_*.svg"))


def test_all_strategies_execute_on_the_same_scenarios(ctx, tmp_path, monkeypatch):
    from app.evaluation import report, runner
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    strategies = runner.default_strategies(True, True)
    assert [s.name for s in strategies] == list(PROGRESSIVE) + list(ABLATIONS)
    res = runner.run_experiment(ctx, strategies, 2, 3, persist=False)
    assert res["status"] == "COMPLETED"
    for name in res["results"]:
        assert set(res["results"][name]) == {1, 2}
    calls = {name: [r["incident"] for r in res["incidents"] if r["strategy"] == name] for name in res["results"]}
    assert len({tuple(v) for v in calls.values()}) == 1          # identical calls replayed for every strategy
    s = res["summary"]
    assert {r["strategy"] for r in s["paired_vs_baseline"]} == {"SEVERITY", "TRAFFIC", "HOSPITAL", "FULL"}
    assert {r["strategy"] for r in s["ablation_vs_full"]} == set(ABLATIONS)
    assert all("insufficient" in (r["note"] or "") for r in s["paired_vs_baseline"])   # n=2: no significance claims
    ai = s["ai_metrics"]["all"]
    assert ai["n"] > 0 and 0 <= ai["accuracy"] <= 1 and ai["brier_multiclass"] >= 0 and "reliability_curve" in ai
    assert "not clinical" in s["ai_metrics"]["label"]
    t = res["results"]["TRAFFIC"][1]                        # traffic prediction scored against the simulated state
    assert t["traffic_pred_samples"] > 0 and 0 <= t["traffic_pred_accuracy"] <= 100
    assert "traffic_pred_accuracy" not in res["results"]["SEVERITY"][1]       # no prediction in that strategy


def test_evaluation_api(client, viewer_headers, dispatcher_headers):
    runs = client.get("/api/evaluation/runs", headers=viewer_headers).json()
    run = next(r for r in runs if r["run_name"] == "pytest-eval")
    d = client.get(f"/api/evaluation/runs/{run['id']}", headers=viewer_headers).json()
    assert d["summary"]["strategies"] == ["BASELINE", "SEVERITY", "FULL"] and d["results_stored"] == 9
    assert "hospital_templates" not in d["metadata"]
    res = client.get(f"/api/evaluation/runs/{run['id']}/results?strategy=FULL", headers=viewer_headers).json()
    assert len(res) == 3 and all(r["metrics"]["calls"] > 0 for r in res)
    assert client.get("/api/evaluation/strategies", headers=viewer_headers).json()["progressive"][0]["name"] == "BASELINE"
    assert client.post("/api/evaluation/runs", json={"scenarios": 1}, headers=dispatcher_headers).status_code == 403
    assert client.get("/api/evaluation/runs/does-not-exist", headers=viewer_headers).status_code == 404


def test_effect_sizes_medians_and_insufficient_samples():
    rng = np.random.default_rng(7)
    ref = {k: float(v) for k, v in enumerate(rng.normal(500, 50, 40))}
    faster = {k: v - 25 - float(rng.exponential(8)) for k, v in ref.items()}
    r = S.paired(ref, faster, "response_time_s")
    assert r["effect_size"] is not None and r["effect_size"] < 0              # strategy - reference: faster -> negative
    assert r["effect_interpretation"] in ("small", "medium", "large")
    assert r["reference_median"] > r["strategy_median"] and r["median_difference"] < 0
    assert r["effect_size_type"] == ("cohens_dz" if r["test"] == "paired t-test" else "rank_biserial")
    assert S.rank_biserial(np.array([1.0, 2.0, -0.5, 3.0])) == pytest.approx(0.8)      # (9 - 1) / 10
    assert S.cohens_dz(np.array([1.0, 2.0, 3.0])) == pytest.approx(2.0)
    small = S.paired({0: 1.0, 1: 3.0, 2: 2.0}, {0: 2.0, 1: 4.0, 2: 2.5}, "response_time_s")
    assert small["p_value"] is None and "insufficient sample size for reliable statistical inference" in small["note"]
    assert S.interpret(0.05, "rank_biserial") == "negligible" and S.interpret(0.9, "cohens_dz") == "large"


def test_new_operational_metrics_are_measured(ctx):
    sc = generate_scenario(ctx.space, 42, 4, ScenarioOptions())
    for strat in (BASELINE, HOSPITAL, FULL):
        sim = Simulation(ctx, sc, strat, _triages(ctx, sc)).run()
        m = scenario_metrics(sim, incident_records(sim, strat.name))
        for k in ("priority_violations", "route_failures", "closure_waits", "unsafe_reallocations"):
            assert isinstance(m[k], int) and m[k] >= 0, k
        if strat.hospital_intelligence and m.get("hospital_pred_samples"):
            assert m["hospital_load_pred_mae"] >= 0 and m["hospital_load_persistence_mae"] >= 0
            assert m["hospital_wait_pred_mae_s"] >= 0
        if strat.traffic_prediction and m.get("traffic_pred_samples"):
            assert 0 <= m["traffic_fallback_accuracy"] <= 100 and m["traffic_fallback_mae"] >= 0
        if not strat.hospital_intelligence:
            assert "hospital_load_pred_mae" not in m                        # no prediction -> nothing to score


def test_priority_violation_definition(ctx):
    center = int(ctx.space.nodes[len(ctx.space.nodes) // 2])
    dist = _nodes_by_distance(ctx, center)
    a, b = dist[3][1], dist[6][1]
    # a CRITICAL call waiting first, then a LOW call; one ICU unit; FIFO baseline serves the CRITICAL one first,
    # so serving the LOW call while the CRITICAL one waits must be counted only when it really happens
    calls = [_incident(ctx, a, CRITICAL_CASE, "CRITICAL", "T-I01", t=0.0), _incident(ctx, b, MILD_CASE, "LOW", "T-I02", t=5.0)]
    sc = _scenario(ctx, [_unit(ctx, "U1", center, "ICU")], calls)
    sim = Simulation(ctx, sc, BASELINE, _triages(ctx, sc)).run()
    assert sim.priority_violations == 0
    sim2 = Simulation(ctx, sc, BASELINE, _triages(ctx, sc))
    sim2.incs[0].state = "WAITING"; sim2.incs[0].release_t = 0.0                     # critical waiting (released)
    sim2.incs[1].state = "WAITING"; sim2.incs[1].release_t = 0.0
    sim2.t = 10.0
    sim2._count_priority_violations([(sim2.incs[1], sim2.units["U1"])])            # unit given to the LOW call
    assert sim2.priority_violations == 1


def test_offline_traffic_evaluation_three_way(monkeypatch, tmp_path):
    import json

    from app.evaluation import traffic_eval
    monkeypatch.setattr(traffic_eval, "OUT", tmp_path / "traffic_prediction.json")
    out = traffic_eval.evaluate()
    assert out["source"] == "SIMULATION" and out["status"] in ("EVALUATED", "INSUFFICIENT DATA")
    if out["status"] == "EVALUATED":
        c = out["comparison"]
        assert set(c) == {"ml", "fallback", "persistence", "best_by_accuracy"}
        assert all(0 <= c[k]["accuracy"] <= 1 for k in ("ml", "fallback", "persistence"))
    else:
        assert out["comparison"] is None
    assert json.loads((tmp_path / "traffic_prediction.json").read_text())["status"] == out["status"]
