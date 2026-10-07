"""CLI for the research evaluation.

  python -m app.evaluation.run_experiment --all --scenarios 100 --seed 42          # systems A-E on the same scenarios
  python -m app.evaluation.run_experiment --ablation --scenarios 100 --seed 42     # FULL and FULL-minus-one
  python -m app.evaluation.run_experiment --all --ablation --scenarios 100         # both in one run (shared scenarios)
  python -m app.evaluation.run_experiment --strategy BASELINE --strategy FULL --scenarios 10

Results: evaluation/results/<run name>/ (CSV, JSON, REPORT.md, plots/*.svg) and the experiment_* tables.
Uses the configured database READ-ONLY for the road network, hospitals and traffic history; writes only to the
experiment_* tables (use --no-db to skip them).
"""
from __future__ import annotations

import argparse
import sys
import time

from app.evaluation.scenarios import ScenarioOptions
from app.evaluation.simulator import SimParams
from app.evaluation.strategies import ALL, resolve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evaluation.run_experiment", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", action="append", default=[], help=f"strategy to run (repeatable): {', '.join(ALL)}")
    ap.add_argument("--all", action="store_true", help="run the five progressive systems BASELINE..FULL")
    ap.add_argument("--ablation", action="store_true", help="run FULL and the six FULL-minus-one ablations")
    ap.add_argument("--scenarios", type=int, default=100, help="number of scenarios (default 100)")
    from app.config import get_settings
    ap.add_argument("--seed", type=int, default=get_settings().evaluation_seed,
                    help="random seed of the scenario generator (default EVALUATION_SEED, 42)")
    ap.add_argument("--name", help="run name (default: derived from kind, seed, size and time)")
    ap.add_argument("--duration-min", type=float, default=30.0, help="call window per scenario, simulated minutes")
    ap.add_argument("--critical-target-s", type=float, default=480.0, help="response target for CRITICAL patients")
    ap.add_argument("--escalation", choices=("reject", "approve"), default="reject",
                    help="simulated dispatcher's answer to an ESCALATED reallocation (default reject)")
    ap.add_argument("--dt", type=float, default=2.0, help="simulation step in simulated seconds")
    osrm = ap.add_mutually_exclusive_group()
    osrm.add_argument("--osrm", dest="osrm", action="store_true", default=None, help="require OSRM alternatives")
    osrm.add_argument("--no-osrm", dest="osrm", action="store_false", help="graph-only routing")
    ap.add_argument("--traffic-model", choices=("trained", "fallback"), default="trained",
                    help="trained = live procedure (learned model only if it beats persistence); fallback = force it")
    ap.add_argument("--no-db", action="store_true", help="do not write the experiment_* tables")
    a = ap.parse_args(argv)

    names = list(a.strategy)
    from app.evaluation.runner import build_context, default_strategies, run_experiment
    strategies = default_strategies(a.all, a.ablation) + [s for s in resolve(names)]
    seen, uniq = set(), []
    for s in strategies:
        if s.name not in seen:
            seen.add(s.name)
            uniq.append(s)
    if not uniq:
        ap.error("choose --all, --ablation and/or --strategy NAME")
    from app.utils.logging import configure_logging
    configure_logging()
    from app.database import run_migrations
    if not a.no_db:
        run_migrations()              # adds the experiment_* tables if this database has not got them yet
    t0 = time.time()
    ctx = build_context(use_osrm=a.osrm, traffic_model=a.traffic_model)
    print(f"Context: city {ctx.info['city']['name']}, network {ctx.info['road_network']}, routing {ctx.info['routing']}, "
          f"traffic model {ctx.info['traffic_model'].get('label')} ({ctx.info['traffic_model'].get('version')})", flush=True)

    def progress(done: int, total: int) -> None:
        if done == total or done % max(1, total // 20) == 0:
            print(f"  {done}/{total} simulations ({time.time() - t0:.0f} s)", flush=True)

    res = run_experiment(ctx, uniq, a.scenarios, a.seed, run_name=a.name,
                         params=SimParams(dt_s=a.dt, critical_target_s=a.critical_target_s, escalation_policy=a.escalation),
                         options=ScenarioOptions(duration_s=a.duration_min * 60, scene_time_s=ctx.settings.scene_time_s,
                                                 handover_time_s=ctx.settings.handover_time_s,
                                                 ed_mean_stay_min=ctx.settings.ed_mean_stay_min),
                         persist=not a.no_db, progress=progress)
    s = res["summary"]
    print(f"\nRun {res['run_name']}: {res['status']} in {time.time() - t0:.0f} s; failures: {len(s['failures'])}")
    print(f"Results: {res['output_dir']}")
    by = {c["metric"]: c for c in s["comparison"]}
    w = max(len(n) for n in s["strategies"])
    print("\n" + "metric".ljust(34) + "".join(n.rjust(max(w, 10) + 2) for n in s["strategies"]))
    for m in ("response_time_s", "patient_wait_s", "critical_delay_s", "critical_delayed_pct", "hospital_wait_simulated_s",
              "reroute_saved_s", "ambulance_utilization", "review_rate", "manual_interventions", "resource_conflicts"):
        c = by[m]
        vals = "".join(("n/a" if v is None else f"{v:.2f}").rjust(max(w, 10) + 2) for v in c["values"].values())
        print(f"{c['label'][:33]:34}{vals}")
    return 0 if res["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
