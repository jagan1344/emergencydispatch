// Research evaluation: stored experiments (progressive systems A-E and FULL-minus-one ablations) with their
// paired statistics. Every number comes from /api/evaluation (experiment_* tables); nothing is computed here
// except formatting.
import { useEffect, useMemo, useState } from "react";
import { Bar as RBar, BarChart, CartesianGrid, ErrorBar, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ErrorNote, Panel } from "../components/ui";
import { api, getUser } from "../services/api";
import { fmtTime } from "../services/format";

const PROGRESSIVE = ["BASELINE", "SEVERITY", "TRAFFIC", "HOSPITAL", "FULL"];
const CHARTS = ["response_time_s", "patient_wait_s", "critical_delay_s", "hospital_wait_simulated_s", "manual_interventions", "reroute_saved_s"];
const TABLE = ["response_time_s", "patient_wait_s", "dispatch_delay_s", "actual_travel_s", "eta_abs_error_s", "reroute_saved_s",
  "reactive_detours", "ambulance_utilization", "no_unit_available_pct", "hospital_wait_simulated_s", "hospital_capability_gap_pct",
  "unsuitable_unit_pct", "critical_response_s", "critical_delay_s", "critical_delayed_pct", "review_rate", "automatic_decision_rate",
  "potentially_inappropriate_auto", "under_triage_pct", "resource_conflicts", "reallocations", "manual_interventions",
  "traffic_pred_accuracy", "traffic_persistence_accuracy", "traffic_fallback_accuracy", "traffic_pred_mae", "traffic_persistence_mae",
  "traffic_fallback_mae", "priority_violations", "route_failures", "closure_waits", "unsafe_reallocations", "replacement_eta_s",
  "hospital_load_pred_mae", "hospital_load_persistence_mae", "hospital_wait_pred_mae_s", "hospital_wait_persistence_mae_s"];
const KEY_TESTS = ["response_time_s", "patient_wait_s", "critical_delay_s", "hospital_wait_simulated_s", "priority_violations",
  "route_failures", "unsuitable_unit_pct", "under_triage_pct", "manual_interventions"];
const pv = (p: number | null | undefined) => (p == null ? "—" : p < 0.0001 ? "<0.0001" : p.toFixed(4));
const eff = (r: any) => (r.effect_size == null ? "—" : `${r.effect_size.toFixed(2)} ${r.effect_size_type === "rank_biserial" ? "r" : r.effect_size_type === "cohens_dz" ? "dz" : ""} (${r.effect_interpretation || "?"})`);

function ModelQuality() {
  const [q, setQ] = useState<any>(null);
  useEffect(() => { api("/api/evaluation/model-quality").then(setQ).catch(() => setQ(null)); }, []);
  if (!q) return null;
  const c = q.severity_calibration, t = q.traffic_prediction;
  const n = (v: any, d = 4) => (v == null ? "—" : Number(v).toFixed(d));
  return (
    <div className="two-col">
      <Panel title="Severity probability calibration (untouched test split)">
        {c.status === "NOT RUN" ? <p className="muted">NOT RUN - <code>{c.how_to_run}</code></p> : (
          <div data-testid="calibration-report">
            <table className="table compact"><thead><tr><th /><th>Raw</th><th>Calibrated</th></tr></thead><tbody>
              <tr><td>Brier score (multiclass, lower = better)</td><td>{n(c.brier_score_raw)}</td><td>{n(c.brier_score_calibrated)}</td></tr>
              <tr><td>ECE (top-label, {c.ece_bins?.split(" ")[0]} bins)</td><td>{n(c.ece_raw)}</td><td>{n(c.ece_calibrated)}</td></tr>
            </tbody></table>
            <div className="small">Status <b>{c.status}</b> · method {c.calibration_method || "none"} · version {c.calibration_version || "—"} ·
              deployed probabilities: {c.deployed_probabilities}</div>
            <div className="muted small">accuracy {n(c.accuracy)} · precision {n(c.precision)} · recall {n(c.recall)} · F1 {n(c.f1)} ·
              test samples {c.sample_count} · calibration samples {c.calibration_sample_count} · dataset {c.dataset}</div>
            <div className="muted small">{c.split}. {c.note}. Model confidence, not clinical certainty.</div>
          </div>)}
      </Panel>
      <Panel title="Traffic prediction: ML vs fallback vs persistence">
        {t.status === "NOT RUN" ? <p className="muted">NOT RUN - <code>{t.how_to_run}</code></p> :
          t.status !== "EVALUATED" ? <p className="muted">{t.status}: {t.reason}</p> : (
          <div data-testid="traffic-comparison">
            <table className="table compact"><thead><tr><th>Method</th><th>Accuracy</th><th>MAE (levels)</th></tr></thead><tbody>
              {["ml", "fallback", "persistence"].map((k) => (
                <tr key={k} className={t.comparison.best_by_accuracy === k ? "selected" : ""}><td>{k === "ml" ? "ML (RandomForest)" : k === "fallback" ? "Rule fallback" : "Persistence (no change)"}</td>
                  <td>{n(t.comparison[k]?.accuracy)}</td><td>{n(t.comparison[k]?.mae_levels)}</td></tr>))}
            </tbody></table>
            <div className="muted small">Source <b>{t.source}</b> traffic history ({t.history_events} events) · {t.samples} samples, {t.holdout_samples} hold-out ·
              horizon {t.horizon_min} min · deployed {t.deployed_method} · {t.note}</div>
            <div className="muted small">{t.reason}. Simulated traffic, not real-time Bengaluru traffic.</div>
          </div>)}
      </Panel>
    </div>
  );
}
const axis = { stroke: "var(--muted)", fontSize: 12 };
const SERIES = "#58a6ff";

function fmt(v: number | null | undefined, unit: string) {
  if (v == null) return "—";
  if (unit === "s") return Math.abs(v) >= 120 ? `${(v / 60).toFixed(1)} min` : `${v.toFixed(0)} s`;
  if (unit === "ratio") return v.toFixed(3);
  if (unit === "%") return `${v.toFixed(1)}%`;
  return v.toFixed(2);
}

function tCI(d: any): [number, number] | null {
  // 95% CI half-width from the stored descriptive statistics (normal approx. for n > 30, t table otherwise)
  if (!d || d.n < 2 || d.mean == null) return null;
  const t = d.n > 30 ? 1.96 : [12.71, 4.30, 3.18, 2.78, 2.57, 2.45, 2.36, 2.31, 2.26, 2.23, 2.20, 2.18, 2.16, 2.14, 2.13, 2.12, 2.11, 2.10, 2.09, 2.09, 2.08, 2.07, 2.07, 2.06, 2.06, 2.06, 2.05, 2.05, 2.05][d.n - 2];
  const h = (t * d.std) / Math.sqrt(d.n);
  return [h, h];
}

export default function Evaluation() {
  const user = getUser();
  const [runs, setRuns] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [run, setRun] = useState<any>(null);
  const [job, setJob] = useState<any>(null);
  const [form, setForm] = useState({ scenarios: 20, seed: 42, progressive: true, ablation: false });
  const [error, setError] = useState<string | null>(null);

  const loadRuns = () => api<any[]>("/api/evaluation/runs").then((r) => {
    setRuns(r);
    setSel((cur) => cur ?? (r.find((x) => x.status !== "RUNNING")?.id || null));
  }).catch((e) => setError(e.message));
  useEffect(() => { loadRuns(); }, []);
  useEffect(() => {
    if (!sel) return;
    setRun(null);
    api(`/api/evaluation/runs/${sel}`).then(setRun).catch((e) => setError(e.message));
  }, [sel]);
  useEffect(() => {   // follow a running job
    if (job?.state !== "RUNNING") return;
    const t = window.setInterval(() => api("/api/evaluation/job").then((j) => {
      setJob(j);
      if (j.state !== "RUNNING") { loadRuns(); if (j.run_id) setSel(j.run_id); }
    }), 2000);
    return () => window.clearInterval(t);
  }, [job?.state]);

  const start = async () => {
    setError(null);
    try { setJob(await api("/api/evaluation/runs", { method: "POST", body: form })); }
    catch (e: any) { setError(e.message); }
  };

  const s = run?.summary;
  const comp = useMemo(() => Object.fromEntries((s?.comparison || []).map((c: any) => [c.metric, c])), [s]);
  const prog = (s?.strategies || []).filter((n: string) => PROGRESSIVE.includes(n));
  const abl = (s?.strategies || []).filter((n: string) => n.startsWith("FULL-"));
  const vsBase = useMemo(() => Object.fromEntries((s?.paired_vs_baseline || []).map((r: any) => [`${r.strategy}|${r.metric}`, r])), [s]);
  const vsFull = (s?.ablation_vs_full || []).filter((r: any) => ["response_time_s", "patient_wait_s", "critical_delay_s",
    "hospital_wait_simulated_s", "reroute_saved_s", "under_triage_pct", "manual_interventions", "unsuitable_unit_pct"].includes(r.metric));

  return (
    <div className="page evaluation" data-testid="evaluation-page">
      <div className="toolbar"><h1>Research evaluation</h1>
        <span className="muted">simulated decision-support experiments - same seeded scenarios replayed under each strategy; not clinical or real-world performance</span></div>
      <ErrorNote error={error} />
      <ModelQuality />
      <div className="two-col">
        <Panel title="Run an experiment">
          {user?.role === "ADMIN" ? (
            <div className="form-grid">
              <label>Scenarios<input type="number" min={1} max={200} value={form.scenarios} onChange={(e) => setForm({ ...form, scenarios: +e.target.value })} /></label>
              <label>Seed<input type="number" min={0} value={form.seed} onChange={(e) => setForm({ ...form, seed: +e.target.value })} /></label>
              <label className="check"><input type="checkbox" checked={form.progressive} onChange={(e) => setForm({ ...form, progressive: e.target.checked })} /> Systems A-E (BASELINE ... FULL)</label>
              <label className="check"><input type="checkbox" checked={form.ablation} onChange={(e) => setForm({ ...form, ablation: e.target.checked })} /> Ablation (FULL minus one capability)</label>
              <div className="wide actions"><button className="primary" disabled={job?.state === "RUNNING"} onClick={start} data-testid="run-experiment">Run experiment</button></div>
              <p className="muted small wide">Runs inside the backend (max 200 scenarios). For 100+ scenarios prefer the CLI:
                <code> python -m app.evaluation.run_experiment --all --ablation --scenarios 100 --seed 42</code></p>
            </div>
          ) : <p className="muted">Experiments are started by an ADMIN (CPU-intensive) or with the CLI; stored results are visible to every role.</p>}
          {job && <div className="muted small" data-testid="experiment-job">Job {job.run_name}: <b>{job.state}</b> {job.total ? `(${job.done}/${job.total} simulations)` : ""} {job.error || ""}</div>}
        </Panel>
        <Panel title="Stored experiments">
          {runs.length === 0 ? <p className="muted">No experiment yet.</p> : (
            <table className="table" data-testid="experiment-runs"><thead><tr><th>Run</th><th>Kind</th><th>Scenarios</th><th>Seed</th><th>Traffic model</th><th>Status</th><th>Started</th></tr></thead>
              <tbody>{runs.map((r) => (
                <tr key={r.id} className={r.id === sel ? "selected" : ""} onClick={() => setSel(r.id)} style={{ cursor: "pointer" }}>
                  <td>{r.run_name}</td><td>{r.kind}</td><td>{r.scenario_count}</td><td>{r.random_seed}</td><td>{r.traffic_model}</td><td>{r.status}</td><td>{fmtTime(r.started_at)}</td></tr>))}
              </tbody></table>)}
        </Panel>
      </div>

      {run && !s && <Panel title={run.run_name}><p className="muted">{run.status === "RUNNING" ? "Running…" : run.error || "No summary stored."}</p></Panel>}
      {s && (<>
        <Panel title={`Configuration - ${run.run_name}`}>
          <div className="muted small" data-testid="experiment-meta">
            seed <b>{run.random_seed}</b> · {run.scenario_count} scenarios · strategies {s.strategies.join(", ")} · code <code>{(run.git_commit || "").slice(0, 10)}</code> ·
            city {run.city} · routing {run.routing?.graph_source} (OSRM: {run.routing?.osrm}) · traffic prediction <b>{s.traffic_model?.label}</b> ({s.traffic_model?.version}) ·
            status {run.status}{s.failures?.length ? ` · ${s.failures.length} failed simulations (see failures.csv)` : ""}
          </div>
          <div className="small" data-testid="experiment-denominators">
            Design: <b>{run.scenario_count} scenarios × {s.strategies.length} strategy configurations = {run.scenario_count * s.strategies.length} simulation runs</b>
            {" "}(every scenario replayed once under every configuration). Unit of analysis: the scenario. Table values are means over the
            scenarios of one configuration (n per metric in the report); paired tests use one pair per scenario.
          </div>
        </Panel>
        {prog.length > 0 && (
          <Panel title="Comparison of systems A-E (mean over scenarios; * = paired test vs BASELINE significant after Holm correction)">
            <div className="table-wrap"><table className="table" data-testid="comparison-table">
              <thead><tr><th>Metric</th>{prog.map((n: string) => <th key={n}>{n}</th>)}</tr></thead>
              <tbody>{TABLE.filter((m) => comp[m]).map((m) => (
                <tr key={m}><td title={`direction: ${comp[m].direction === -1 ? "lower is better" : comp[m].direction === 1 ? "higher is better" : "no preferred direction"}`}>{comp[m].label}</td>
                  {prog.map((n: string) => {
                    const t = vsBase[`${n}|${m}`];
                    return <td key={n} title={t ? `diff ${t.mean_difference?.toFixed(2)} · ${t.test || "no test"}${t.p_adjusted != null ? ` · p(Holm)=${t.p_adjusted.toFixed(4)}` : ""}${t.note ? ` · ${t.note}` : ""}` : ""}>
                      {fmt(comp[m].values[n], comp[m].unit)}{t?.significant ? " *" : ""}</td>;
                  })}</tr>))}</tbody></table></div>
          </Panel>)}
        {prog.length > 1 && (
          <div className="grid-3">
            {CHARTS.filter((m) => comp[m]).map((m) => {
              const data = prog.map((n: string) => ({ name: n, mean: comp[m].values[n], err: tCI(s.descriptive?.[n]?.[m]), n: s.descriptive?.[n]?.[m]?.n }));
              return (
                <Panel key={m} title={`${comp[m].label} (${comp[m].unit})`}>
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="var(--grid)" vertical={false} /><XAxis dataKey="name" {...axis} /><YAxis {...axis} />
                      <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }}
                        formatter={(v: any, _k: any, p: any) => [`${fmt(v, comp[m].unit)} (n=${p.payload.n}; whisker = 95% CI)`, "mean"]} />
                      <RBar dataKey="mean" fill={SERIES} radius={[4, 4, 0, 0]} maxBarSize={48} isAnimationActive={false}>
                        <ErrorBar dataKey="err" width={6} stroke="var(--text)" />
                      </RBar>
                    </BarChart>
                  </ResponsiveContainer>
                </Panel>);
            })}
          </div>)}
        {abl.length > 0 && (
          <Panel title="Ablation: FULL minus one capability vs FULL (paired, Holm-adjusted)">
            <div className="table-wrap"><table className="table" data-testid="ablation-table">
              <thead><tr><th>Configuration</th><th>Metric</th><th>FULL</th><th>Ablated</th><th>Median FULL / abl.</th><th>Mean diff</th><th>95% CI</th><th>Test</th><th>p raw</th><th>p (Holm)</th><th>Effect size</th></tr></thead>
              <tbody>{vsFull.map((r: any) => (
                <tr key={`${r.strategy}|${r.metric}`}><td>{r.strategy}</td><td>{comp[r.metric]?.label}</td>
                  <td>{fmt(r.reference_mean, comp[r.metric]?.unit)}</td><td>{fmt(r.strategy_mean, comp[r.metric]?.unit)}</td>
                  <td>{fmt(r.reference_median, comp[r.metric]?.unit)} / {fmt(r.strategy_median, comp[r.metric]?.unit)}</td>
                  <td>{r.mean_difference == null ? "—" : r.mean_difference.toFixed(2)}</td>
                  <td>{r.ci95_low == null ? "—" : `[${r.ci95_low.toFixed(1)}, ${r.ci95_high.toFixed(1)}]`}</td>
                  <td>{r.test || "—"}{r.note ? <span className="muted small"> ({r.note})</span> : null}</td>
                  <td>{pv(r.p_value)}</td>
                  <td>{r.p_adjusted == null ? "—" : `${pv(r.p_adjusted)}${r.significant ? " *" : ""}`}</td><td>{eff(r)}</td></tr>))}</tbody></table></div>
          </Panel>)}
        {prog.length > 1 && (s?.paired_vs_baseline || []).length > 0 && (
          <Panel title="Statistical tests: each system vs BASELINE (paired by scenario, Holm-adjusted)">
            <div className="table-wrap"><table className="table" data-testid="stat-tests">
              <thead><tr><th>System</th><th>Metric</th><th>Median BASELINE</th><th>Median system</th><th>Median diff</th><th>Test</th><th>p raw</th><th>p (Holm)</th><th>Effect size</th></tr></thead>
              <tbody>{(s.paired_vs_baseline as any[]).filter((r) => KEY_TESTS.includes(r.metric)).map((r) => (
                <tr key={`${r.strategy}|${r.metric}`}><td>{r.strategy}</td><td>{comp[r.metric]?.label || r.metric}</td>
                  <td>{fmt(r.reference_median, comp[r.metric]?.unit)}</td><td>{fmt(r.strategy_median, comp[r.metric]?.unit)}</td>
                  <td>{r.median_difference == null ? "—" : r.median_difference.toFixed(2)}</td>
                  <td>{r.test || "—"}{r.note ? <span className="muted small"> ({r.note})</span> : null}</td>
                  <td>{pv(r.p_value)}</td><td>{r.p_adjusted == null ? "—" : `${pv(r.p_adjusted)}${r.significant ? " *" : ""}`}</td>
                  <td>{eff(r)}</td></tr>))}</tbody></table></div>
            <p className="muted small">Effect size: rank-biserial r for Wilcoxon (|r| 0.1 small, 0.3 medium, 0.5 large); Cohen's dz for paired t. Unit of analysis = scenario.</p>
          </Panel>)}
      </>)}
    </div>
  );
}
