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
  "potentially_inappropriate_auto", "under_triage_pct", "resource_conflicts", "reallocations", "manual_interventions"];
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
              <thead><tr><th>Configuration</th><th>Metric</th><th>FULL</th><th>Ablated</th><th>Mean diff</th><th>95% CI</th><th>Test</th><th>p (Holm)</th></tr></thead>
              <tbody>{vsFull.map((r: any) => (
                <tr key={`${r.strategy}|${r.metric}`}><td>{r.strategy}</td><td>{comp[r.metric]?.label}</td>
                  <td>{fmt(r.reference_mean, comp[r.metric]?.unit)}</td><td>{fmt(r.strategy_mean, comp[r.metric]?.unit)}</td>
                  <td>{r.mean_difference == null ? "—" : r.mean_difference.toFixed(2)}</td>
                  <td>{r.ci95_low == null ? "—" : `[${r.ci95_low.toFixed(1)}, ${r.ci95_high.toFixed(1)}]`}</td>
                  <td>{r.test || "—"}{r.note ? <span className="muted small"> ({r.note})</span> : null}</td>
                  <td>{r.p_adjusted == null ? "—" : `${r.p_adjusted.toFixed(4)}${r.significant ? " *" : ""}`}</td></tr>))}</tbody></table></div>
          </Panel>)}
      </>)}
    </div>
  );
}
