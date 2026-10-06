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
    out = [f"### {title}", "", "| Comparison | Metric | Pairs | Mean diff | 95% CI | Test | p (Holm) | Significant |",
           "|---|---|---:|---:|---|---|---:|---|"]
    for r in rows:
        if r["metric"] not in ("response_time_s", "patient_wait_s", "critical_delay_s", "critical_delayed_pct",
                               "hospital_wait_simulated_s", "time_to_treatment_s", "reroute_saved_s",
                               "manual_interventions", "hospital_capability_gap_pct", "under_triage_pct",
                               "eta_abs_error_s", "reactive_detours"):
            continue
        ci = "n/a" if r["ci95_low"] is None else f"[{r['ci95_low']:.1f}, {r['ci95_high']:.1f}]"
        p = "n/a" if r.get("p_adjusted") is None else f"{r['p_adjusted']:.4f}"
        sig = {True: "yes", False: "no", None: "–"}[r.get("significant")]
        test = r["test"] or "none"
        if r.get("note"):
            test += f" ({r['note']})"
        md = "n/a" if r["mean_difference"] is None else f"{r['mean_difference']:+.2f}"
        out.append(f"| {r['strategy']} vs {r['reference']} | {METRICS[r['metric']][0]} | {r['pairs']} | {md} | {ci} | {test} | {p} | {sig} |")
    return out + [""]


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
                    "mean_difference", "pct_change", "improvement", "ci95_low", "ci95_high", "ci_method", "test",
                    "statistic", "p_value", "p_adjusted", "significant", "normality_p", "note"])
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
          f"* Started {meta.get('started_at')}, completed {meta.get('completed_at', 'n/a')}", "",
          "## Comparison (mean over scenarios)", ""]
    main = [m for m in METRICS if m not in ("decision_ms",)]
    md += _md_table(summary, names, main) + [""]
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
