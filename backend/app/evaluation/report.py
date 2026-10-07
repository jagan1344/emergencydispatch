"""Experiment output: machine-readable CSV/JSON, a Markdown report and SVG figures - all generated from the
measured results (nothing is typed in by hand). Written to evaluation/results/<run_name>/."""
from __future__ import annotations

import csv
import json
import math
from html import escape
from pathlib import Path

from app.evaluation.metrics import METRICS
from app.evaluation.runner import RESULTS_DIR, jsonable
from app.evaluation.scenarios import summary as scenario_summary

# figure tokens (light surface; validated single-hue palette slot 1 + neutral ink)
SURFACE, INK, INK_2, MUTED, GRID, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#2a78d6"

FIGURES = [("response_time_s", "progressive"), ("patient_wait_s", "progressive"), ("critical_delay_s", "progressive"),
           ("critical_delayed_pct", "progressive"), ("hospital_wait_simulated_s", "progressive"),
           ("manual_interventions", "progressive"), ("reroute_saved_s", "progressive"),
           ("ambulance_utilization", "progressive"), ("response_time_s", "ablation"), ("critical_delay_s", "ablation"),
           ("hospital_wait_simulated_s", "ablation")]


def _fmt(v, unit: str = "") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if unit == "ratio":
        return f"{v:.3f}"
    if unit == "s" and abs(v) >= 120:
        return f"{v:.0f} s ({v / 60:.1f} min)"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})


def bar_chart_svg(title: str, subtitle: str, labels: list[str], means: list[float | None],
                  lows: list[float | None], highs: list[float | None], unit: str, ns: list[int]) -> str:
    """Vertical bars (one series, one hue), 95 % CI whiskers, value labels, native hover tooltips."""
    W, H, L, R, T, B = 760, 420, 70, 20, 70, 90
    vals = [v for v in means + highs if v is not None]
    raw_top = max(vals + [0.0]) * 1.08 or 1.0
    raw_bot = min([v for v in means + lows if v is not None] + [0.0])
    step = _nice_step((raw_top - min(raw_bot, 0.0)) / 4)
    top = math.ceil(raw_top / step) * step
    bot = math.floor(raw_bot / step) * step if raw_bot < 0 else 0.0
    ph = H - T - B
    y = lambda v: T + ph * (top - v) / (top - bot)
    slot = (W - L - R) / max(1, len(labels))
    bw = min(56.0, slot * 0.55)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'font-family="system-ui, -apple-system, Segoe UI, sans-serif" role="img" aria-label="{escape(title)}">',
           f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
           f'<text x="{L}" y="28" font-size="16" font-weight="600" fill="{INK}">{escape(title)}</text>',
           f'<text x="{L}" y="48" font-size="12" fill="{INK_2}">{escape(subtitle)}</text>']
    for k in range(int(round((top - bot) / step)) + 1):   # recessive grid on round values
        v = bot + step * k
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{L - 8}" y="{y(v) + 4:.1f}" font-size="11" text-anchor="end" fill="{MUTED}">{v:g}</text>')
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="{INK_2}" stroke-width="1"/>')
    for k, (lab, m, lo, hi, n) in enumerate(zip(labels, means, lows, highs, ns)):
        cx = L + slot * (k + 0.5)
        out.append(f'<text x="{cx:.1f}" y="{H - B + 18}" font-size="11" text-anchor="middle" fill="{INK_2}">{escape(lab)}</text>')
        out.append(f'<text x="{cx:.1f}" y="{H - B + 33}" font-size="10" text-anchor="middle" fill="{MUTED}">n={n}</text>')
        if m is None:
            out.append(f'<text x="{cx:.1f}" y="{y(0) - 6:.1f}" font-size="11" text-anchor="middle" fill="{MUTED}">n/a</text>')
            continue
        y0, y1 = sorted((y(0), y(m)))
        tip = f"{lab}: mean {_fmt(m, unit)}" + (f", 95% CI [{_fmt(lo, unit)}, {_fmt(hi, unit)}]" if lo is not None else "") + f", n={n}"
        out.append(f'<g><title>{escape(tip)}</title>'
                   f'<rect x="{cx - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(y1 - y0, 0.5):.1f}" rx="4" fill="{SERIES}"/></g>')
        if lo is not None and hi is not None:
            out.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(hi):.1f}" y2="{y(lo):.1f}" stroke="{INK}" stroke-width="1.5"/>')
            for yy in (y(hi), y(lo)):
                out.append(f'<line x1="{cx - 6:.1f}" x2="{cx + 6:.1f}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{INK}" stroke-width="1.5"/>')
        ly = min(y(m), y(hi) if hi is not None else y(m)) - 8
        out.append(f'<text x="{cx:.1f}" y="{ly:.1f}" font-size="11" text-anchor="middle" fill="{INK}">{m:.3g}</text>')
    out.append(f'<text x="{L}" y="{H - 30}" font-size="10" fill="{MUTED}">Bars: mean over scenarios. Whiskers: 95% CI of the mean (t-interval).</text>')
    out.append(f'<text x="{L}" y="{H - 16}" font-size="10" fill="{MUTED}">Simulated decision-support experiment - not clinical or real-world performance.</text>')
    out.append("</svg>")
    return "\n".join(out)


def _nice_step(span: float) -> float:
    if span <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(span))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= span)


def _ci(desc: dict) -> tuple[float | None, float | None]:
    from scipy import stats as sps
    n, m, sd = desc["n"], desc["mean"], desc["std"]
    if not n or m is None or n < 2:
        return None, None
    h = float(sps.t.ppf(0.975, n - 1) * sd / math.sqrt(n))
    return m - h, m + h


def _figures(out: Path, summary: dict) -> list[str]:
    from app.evaluation.strategies import ABLATIONS, PROGRESSIVE
    figs = out / "plots"
    figs.mkdir(exist_ok=True)
    names = summary["strategies"]
    written = []
    for metric, family in FIGURES:
        group = [n for n in names if n in PROGRESSIVE] if family == "progressive" else \
            [n for n in names if n == "FULL" or n in ABLATIONS]
        if len(group) < 2:
            continue
        label, unit, direction = METRICS[metric]
        desc = [summary["descriptive"][n][metric] for n in group]
        cis = [_ci(d) for d in desc]
        better = {-1: "lower is better", 1: "higher is better", 0: "no preferred direction"}[direction]
        svg = bar_chart_svg(f"{label} by strategy" + (" (ablation)" if family == "ablation" else ""),
                            f"Unit: {unit}; {better}; seed {summary.get('seed')}, {summary.get('scenario_count')} scenarios",
                            [g.replace("FULL-", "−") if family == "ablation" and g != "FULL" else g for g in group],
                            [d["mean"] for d in desc], [c[0] for c in cis], [c[1] for c in cis], unit, [d["n"] for d in desc])
        name = f"{family}_{metric}.svg"
        (figs / name).write_text(svg, encoding="utf-8")
        written.append(f"plots/{name}")
    return written


def _md_table(summary: dict, names: list[str], metrics: list[str]) -> list[str]:
    rows = ["| Metric | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    by = {c["metric"]: c for c in summary["comparison"]}
    for m in metrics:
        c = by[m]
        rows.append(f"| {c['label']} ({c['unit']}) | " + " | ".join(_fmt(c["values"][n], c["unit"]) for n in names) + " |")
    return rows


def _md_tests(rows: list[dict], title: str) -> list[str]:
    if not rows:
        return []
    out = [f"### {title}", "", "| Comparison | Metric | Pairs | Median ref | Median strat | Mean diff | 95% CI | Test | p (raw) | "
           "p (Holm) | Effect size | Significant |", "|---|---|---:|---:|---:|---:|---|---|---:|---:|---|---|"]
    for r in rows:
        if r["metric"] not in ("response_time_s", "patient_wait_s", "critical_delay_s", "critical_delayed_pct",
                               "hospital_wait_simulated_s", "time_to_treatment_s", "reroute_saved_s",
                               "manual_interventions", "hospital_capability_gap_pct", "under_triage_pct",
                               "eta_abs_error_s", "reactive_detours", "priority_violations", "route_failures",
                               "unsafe_reallocations", "hospital_load_pred_mae", "traffic_pred_accuracy"):
            continue
        ci = "n/a" if r["ci95_low"] is None else f"[{r['ci95_low']:.1f}, {r['ci95_high']:.1f}]"
        p = "n/a" if r.get("p_adjusted") is None else f"{r['p_adjusted']:.4f}"
        sig = {True: "yes", False: "no", None: "–"}[r.get("significant")]
        test = r["test"] or "none"
        if r.get("note"):
            test += f" ({r['note']})"
        md = "n/a" if r["mean_difference"] is None else f"{r['mean_difference']:+.2f}"
        f = lambda v: "n/a" if v is None else f"{v:.2f}"
        praw = "n/a" if r.get("p_value") is None else f"{r['p_value']:.4g}"
        eff = "n/a" if r.get("effect_size") is None else f"{r['effect_size']:+.2f} ({r['effect_size_type']}, {r['effect_interpretation']})"
        out.append(f"| {r['strategy']} vs {r['reference']} | {METRICS[r['metric']][0]} | {r['pairs']} | {f(r.get('reference_median'))} | "
                   f"{f(r.get('strategy_median'))} | {md} | {ci} | {test} | {praw} | {p} | {eff} | {sig} |")
    return out + [""]


DESIGN_START, DESIGN_END = "<!-- design-and-denominators:start -->", "<!-- design-and-denominators:end -->"


def design_section(summary: dict, scenario_rows: list[dict]) -> list[str]:
    """What every count in the report refers to - generated from the stored results, never typed in."""
    from app.evaluation.strategies import ABLATIONS, PROGRESSIVE
    names = summary["strategies"]
    n_sc = summary.get("scenario_count") or len(scenario_rows)
    prog = [n for n in names if n in PROGRESSIVE]
    abl = [n for n in names if n in ABLATIONS]
    failures = summary.get("failures") or []
    runs = n_sc * len(names)
    calls = sum(int(r["incidents"]) for r in scenario_rows)
    uncertain = sum(int(r.get("uncertain_reports") or 0) for r in scenario_rows)
    desc = summary["descriptive"]
    out = [DESIGN_START, "## Design and denominators", "",
           "**Unit of analysis: the scenario.** Every number in this report is derived from the counts below.", "",
           "| Quantity | Value | How it is obtained |", "|---|---:|---|",
           f"| Scenarios | {n_sc} | generated once from seed {summary.get('seed')}; scenario *k* uses RNG seed "
           f"({summary.get('seed')}, *k*) and has a stored fingerprint |",
           f"| Strategy configurations | {len(names)} | {len(prog)} progressive systems ({', '.join(prog) or '-'})"
           + (f" + {len(abl)} ablations of FULL ({', '.join(abl)})" if abl else "")
           + ("; FULL is run once and serves as the last progressive system **and** the ablation reference" if abl and "FULL" in prog else "")
           + " |",
           f"| **Simulation runs** | **{runs}** | {n_sc} scenarios × {len(names)} configurations: every scenario is replayed "
           f"once under every configuration (same patients, fleet, hospitals, traffic, disruptions; only the decision "
           f"strategy differs) |",
           f"| Failed runs | {len(failures)} | recorded in failures.csv and excluded from that configuration's statistics |",
           f"| Emergency calls per configuration | {calls} | sum of the calls of the {n_sc} scenarios ({uncertain} with an "
           f"uncertain report); identical for every configuration |",
           f"| Simulated call outcomes | {calls * len(names)} | {calls} calls × {len(names)} configurations "
           f"(incident_results.csv) |", "",
           "**How the statistics use these runs.**",
           f"* *Comparison table*: each cell is the mean over the scenarios of one configuration (scenario-level value = "
           f"mean over that scenario's calls, or a count/share for the scenario). It is NOT a mean over {runs} runs.",
           f"* *Paired tests*: one pair per scenario (the same scenario under two configurations), so at most {n_sc} pairs "
           f"per test; the runs of different configurations are never pooled. Families: "
           + "; ".join(x for x in [
               f"vs BASELINE ({len([n for n in prog if n != 'BASELINE'])} comparisons)" if "BASELINE" in prog and len(prog) > 1 else "",
               f"incremental, each system vs the previous one ({max(0, len(prog) - 1)})" if len(prog) > 1 else "",
               f"ablation vs FULL ({len(abl)})" if abl else ""] if x)
           + ". Holm correction is applied within each comparison over all metrics.",
           "* A metric that does not exist in a scenario (e.g. no CRITICAL patient, no re-route) is NULL there, so its "
           "*n* is smaller than the number of scenarios:", ""]
    ref = "FULL" if "FULL" in names else names[0]
    short = [(m, desc[ref][m]["n"]) for m in METRICS if desc[ref].get(m) and desc[ref][m]["n"] < n_sc]
    if short:
        out += ["| Metric | scenarios with a value (" + ref + ") | why |", "|---|---:|---|"]
        why = {  # most specific prefix first
               "critical_delay_avoided": "scenarios with an executed reallocation where a free alternative unit existed "
                                         "(otherwise the avoided delay is undefined)",
               "donor_delay": "scenarios with an executed reallocation",
               "critical": "scenarios with at least one patient whose simulated label is CRITICAL",
               "reroute_improvement": "scenarios with an accepted re-route with a finite old ETA",
               "hospital_wait_predicted": "scenarios where the hospital prediction was used",
               "traffic": "scenarios with at least one traffic prediction whose horizon ended inside the run"}
        for m, n in short:
            reason = next((v for k, v in why.items() if m.startswith(k)), "scenarios where the metric is measurable")
            out.append(f"| {METRICS[m][0]} | {n} | {reason} |")
    else:
        out.append(f"All metrics of {ref} have a value in every scenario.")
    out += ["", DESIGN_END, ""]
    return out


def refresh_design_section(run_dir: Path) -> Path:
    """Insert / replace the design section of an existing REPORT.md from its stored summary.json and
    scenarios_summary.csv (no re-simulation, no hand editing)."""
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader((run_dir / "scenarios_summary.csv").open(encoding="utf-8")))
    md = (run_dir / "REPORT.md").read_text(encoding="utf-8")
    block = "\n".join(design_section(summary, rows))
    if DESIGN_START in md:
        md = md[:md.index(DESIGN_START)] + block + md[md.index(DESIGN_END) + len(DESIGN_END) + 1:]
    else:
        anchor = "## Comparison (mean over scenarios)"
        md = md.replace(anchor, block + "\n" + anchor, 1)
    (run_dir / "REPORT.md").write_text(md, encoding="utf-8")
    return run_dir / "REPORT.md"


def write_report(run_name: str, summary: dict, results: dict, incidents: list[dict], scenarios: list[dict],
                 meta: dict, config: dict) -> Path:
    out = RESULTS_DIR / run_name
    out.mkdir(parents=True, exist_ok=True)
    names = summary["strategies"]
    (out / "run_metadata.json").write_text(json.dumps(jsonable({**meta, "configuration": config}), indent=2, default=str),
                                           encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(jsonable(summary), indent=2, default=str), encoding="utf-8")
    (out / "scenarios.json").write_text(json.dumps(jsonable(scenarios), default=str), encoding="utf-8")
    _write_csv(out / "scenarios_summary.csv",
               [{"scenario": s["number"], "fingerprint": s["fingerprint"], **scenario_summary(s)} for s in scenarios])
    sc_rows = [{"strategy": n, "scenario": k, **{m: v for m, v in row.items() if not isinstance(v, dict)}}
               for n in names for k, row in sorted(results[n].items())]
    _write_csv(out / "scenario_results.csv", sc_rows, ["strategy", "scenario"] + [m for m in METRICS] +
               sorted({k for r in sc_rows for k in r} - set(METRICS) - {"strategy", "scenario"}))
    if incidents:
        _write_csv(out / "incident_results.csv", incidents, list(incidents[0]))
    comp_rows = [{"metric": c["metric"], "label": c["label"], "unit": c["unit"],
                  "direction": {-1: "lower_is_better", 1: "higher_is_better", 0: "none"}[c["direction"]],
                  **{n: c["values"][n] for n in names}} for c in summary["comparison"]]
    _write_csv(out / "comparison_summary.csv", comp_rows, ["metric", "label", "unit", "direction", *names])
    _write_csv(out / "statistics.csv", [{"strategy": n, "metric": m, **d} for n in names
                                        for m, d in summary["descriptive"][n].items()],
               ["strategy", "metric", "n", "mean", "median", "std", "min", "max", "p25", "p75"])
    tests = [dict(r, family=fam) for fam in ("paired_vs_baseline", "incremental", "ablation_vs_full")
             for r in summary.get(fam, [])]
    if tests:
        _write_csv(out / "paired_tests.csv", tests,
                   ["family", "strategy", "reference", "metric", "pairs", "reference_mean", "strategy_mean",
                    "reference_median", "strategy_median", "median_difference", "mean_difference", "pct_change",
                    "improvement", "ci95_low", "ci95_high", "ci_method", "test", "statistic", "p_value", "p_adjusted",
                    "significant", "effect_size", "effect_size_type", "effect_interpretation", "normality_p", "note"])
    if summary["failures"]:
        _write_csv(out / "failures.csv", summary["failures"], ["scenario", "strategy", "error"])
    figs = _figures(out, summary)

    tm = meta.get("traffic_model") or {}
    md = [f"# Experiment {run_name}", "",
          "Simulated decision-support experiment. Values are measured in a deterministic simulation on the real road "
          "graph with simulated patients, traffic and hospital load; they are not clinical outcomes or real-world "
          "emergency-service performance.", "",
          f"* Status: **{summary.get('status')}**, failures: {len(summary['failures'])}",
          f"* Seed: {meta.get('seed')}, scenarios: {meta.get('scenario_count')}, strategies: {', '.join(names)}",
          f"* Code: `{meta.get('git_commit')}` (branch {meta.get('git_branch')}, uncommitted changes: {meta.get('git_dirty')})",
          f"* City: {meta['city']['name']}, road network: {meta['road_network']['source']} "
          f"({meta['road_network']['nodes']} nodes, {meta['road_network']['edges']} edges, sha {meta['road_network']['sha256']}), "
          f"OSRM: {meta['routing']['osrm']}",
          f"* Severity model: {meta['severity_model']['version']} (dataset: {meta['severity_model']['dataset']})",
          f"* Traffic prediction: **{tm.get('label', 'n/a')}** ({tm.get('version', '')}) - {(tm.get('metrics') or {}).get('reason', '')}",
          f"* Hospital prediction: {meta['hospital_model']['version']} - {meta['hospital_model']['label']}",
          f"* Started {meta.get('started_at')}, completed {meta.get('completed_at', 'n/a')}", ""]
    md += design_section(summary, [{"incidents": len(sc["incidents"]),
                                    "uncertain_reports": sum(1 for i in sc["incidents"] if i.get("report_quality") == "UNCERTAIN")}
                                   for sc in scenarios])
    md += ["## Comparison (mean over scenarios)", ""]
    main = [m for m in METRICS if m not in ("decision_ms",)]
    md += _md_table(summary, names, main) + [""]
    ai = summary.get("ai_metrics") or {}
    if ai.get("all"):
        md += ["## Severity model on the scenario patients", "",
               f"Reference: {ai['label']}. Triage is identical under every strategy.", "",
               "| Calls | n | Accuracy | Precision (macro) | Recall (macro) | F1 (macro) | Brier | ECE | Median confidence | Share < 0.75 |",
               "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for k, lab in (("all", "all"), ("clear_reports", "clear reports"), ("uncertain_reports", "uncertain reports")):
            m = ai.get(k)
            if m:
                cd = m["confidence_distribution"]
                md.append(f"| {lab} | {m['n']} | {m['accuracy']:.3f} | {m['precision_macro']:.3f} | {m['recall_macro']:.3f} | "
                          f"{m['f1_macro']:.3f} | {m['brier_multiclass']:.3f} | {m['ece_top_label']:.3f} | {cd['median']:.3f} | "
                          f"{cd['share_below_0_75']:.3f} |")
        md.append("")
    md += _md_tests(summary.get("paired_vs_baseline", []), "Paired comparisons against BASELINE")
    md += _md_tests(summary.get("incremental", []), "Incremental contribution (each system vs the previous one)")
    md += _md_tests(summary.get("ablation_vs_full", []), "Ablation (FULL minus one capability vs FULL)")
    if figs:
        md += ["## Figures", ""] + [f"![{f}]({f})" for f in figs] + [""]
    md += ["## Files", "", "`comparison_summary.csv`, `scenario_results.csv`, `incident_results.csv`, `statistics.csv`, "
           "`paired_tests.csv`, `scenarios_summary.csv`, `scenarios.json` (complete scenario definitions), "
           "`summary.json`, `run_metadata.json`.", "",
           "Metric definitions: `backend/app/evaluation/metrics.py`; test selection: `backend/app/evaluation/statistics.py`."]
    (out / "REPORT.md").write_text("\n".join(md), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    for name in sys.argv[1:]:
        print(refresh_design_section(RESULTS_DIR / name))
