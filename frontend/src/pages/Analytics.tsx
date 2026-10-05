import { useEffect, useState } from "react";
import { Bar as RBar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import { Kpi, Panel } from "../components/ui";
import { api } from "../services/api";
import { fmtMin, fmtTime } from "../services/format";

const SEV = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
const SEV_C: Record<string, string> = { LOW: "#3fb950", MEDIUM: "#d29922", HIGH: "#f0883e", CRITICAL: "#f85149", UNCLASSIFIED: "#8b949e" };
const axis = { stroke: "var(--muted)", fontSize: 12 };

export default function Analytics() {
  const [s, setS] = useState<any>(null);
  const [b, setB] = useState<any>(null);
  const [rt, setRt] = useState<any>(null);
  const [rr, setRr] = useState<any[]>([]);
  const [ml, setMl] = useState<any>(null);
  useEffect(() => {
    const load = () => {
      api("/api/analytics/summary").then(setS);
      api("/api/analytics/breakdowns").then(setB);
      api("/api/analytics/response-times").then(setRt);
      api("/api/analytics/reroutes").then(setRr);
    };
    load();
    api("/api/ml/model").then(setMl);
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, []);
  if (!s || !b || !rt) return <div className="page">Loading…</div>;
  const scatter = rt.items.filter((i: any) => i.response_s != null).map((i: any) => ({
    t: new Date(i.created_at).getTime(), min: +(i.response_s / 60).toFixed(2), sev: i.severity, src: i.source, ref: i.reference }));
  const live = scatter.filter((p: any) => p.src !== "HISTORICAL_SEED");
  const hist = scatter.filter((p: any) => p.src === "HISTORICAL_SEED");
  const rf = ml?.metrics?.models?.random_forest;
  return (
    <div className="page analytics">
      <div className="toolbar"><h1>Analytics</h1><span className="muted">all durations in {s.time_basis}; seed history is synthetic and shown separately</span></div>
      <div className="kpis">
        <Kpi label="Avg response (live)" value={fmtMin(s.avg_response_time_s)} sub={`${s.response_samples} incidents`} />
        <Kpi label="Avg response (seed history)" value={fmtMin(s.historical_seed.avg_response_time_s)} sub={`${s.historical_seed.incidents} synthetic records`} />
        <Kpi label="Avg dispatch time" value={fmtMin(s.avg_dispatch_time_s)} sub={`decision ${s.avg_decision_ms ?? "—"} ms`} />
        <Kpi label="Route efficiency" value={s.avg_route_efficiency != null ? `${(s.avg_route_efficiency * 100).toFixed(1)}%` : "—"} sub={`shortest / actual, ${s.routes_measured} routes`} />
        <Kpi label="Re-routes" value={s.reroutes} sub={`time saved ${fmtMin(s.reroute_time_saved_s)}`} />
        <Kpi label="Fleet utilization" value={`${s.fleet_utilization_pct}%`} sub={`${s.completed_live} live completed`} />
      </div>
      <div className="grid-2">
        <Panel title="Response time per incident (minutes)">
          <ResponsiveContainer width="100%" height={260}>
            <ScatterChart margin={{ left: 0, right: 10, top: 10 }}>
              <CartesianGrid stroke="var(--grid)" />
              <XAxis dataKey="t" type="number" domain={["auto", "auto"]} tickFormatter={(v) => new Date(v).toLocaleDateString()} {...axis} name="created" />
              <YAxis dataKey="min" {...axis} name="minutes" />
              <Tooltip formatter={(v: any) => v} labelFormatter={() => ""} contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
              <Legend />
              <Scatter name="historical seed (synthetic)" data={hist} fill="#8b949e" />
              <Scatter name="live / simulation" data={live} fill="#58a6ff" />
            </ScatterChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Severity distribution">
          <ResponsiveContainer width="100%" height={260}>
            <PieChart>
              <Pie data={[...b.by_severity].sort((x: any, y: any) => SEV.indexOf(x.key) - SEV.indexOf(y.key))} dataKey="count" nameKey="key" outerRadius={95} label>
                {b.by_severity.map((x: any) => <Cell key={x.key} fill={SEV_C[x.key] || "#888"} />)}
              </Pie>
              <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} /><Legend />
            </PieChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Incidents by type">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={b.by_type}><CartesianGrid stroke="var(--grid)" /><XAxis dataKey="key" {...axis} /><YAxis {...axis} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} /><RBar dataKey="count" fill="#58a6ff" /></BarChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Average response by severity (min)">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={rt.by_severity.map((r: any) => ({ ...r, min: +(r.avg_response_s / 60).toFixed(2) })).sort((x: any, y: any) => SEV.indexOf(x.severity) - SEV.indexOf(y.severity))}>
              <CartesianGrid stroke="var(--grid)" /><XAxis dataKey="severity" {...axis} /><YAxis {...axis} />
              <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
              <RBar dataKey="min">{rt.by_severity.map((r: any) => <Cell key={r.severity} fill={SEV_C[r.severity] || "#888"} />)}</RBar></BarChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Ambulance utilization (dispatches)">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={b.ambulance_utilization}><CartesianGrid stroke="var(--grid)" /><XAxis dataKey="ambulance_id" {...axis} interval={1} /><YAxis {...axis} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} /><RBar dataKey="dispatches_total" fill="#a371f7" /></BarChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Hospital load (%)">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={b.hospital_load.map((h: any) => ({ ...h, load_pct: +h.load_pct }))} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid stroke="var(--grid)" /><XAxis type="number" domain={[0, 100]} {...axis} /><YAxis type="category" dataKey="id" {...axis} />
              <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} /><RBar dataKey="load_pct" fill="#f0883e" /></BarChart>
          </ResponsiveContainer>
        </Panel>
        <Panel title="Re-routing log">
          <table className="table compact"><thead><tr><th>Time</th><th>Incident</th><th>Unit</th><th>Reason</th><th>Old → new ETA</th><th>Saved</th></tr></thead>
            <tbody>{rr.map((r) => <tr key={r.id}><td>{fmtTime(r.created_at)}</td><td>{r.reference}</td><td>{r.ambulance_id}</td><td>{r.reroute_reason}</td>
              <td>{r.old_eta_s != null ? fmtMin(r.old_eta_s) : "∞"} → {fmtMin(r.new_eta_s)}</td><td>{fmtMin(r.time_saved_s)}</td></tr>)}
              {!rr.length && <tr><td colSpan={6} className="muted">No re-routes yet</td></tr>}</tbody></table>
        </Panel>
        <Panel title="Severity model (synthetic data, held-out 20%)">
          {rf ? (<>
            <div className="muted small">{ml.metrics.disclaimer}</div>
            <table className="table compact"><thead><tr><th>Model</th><th>Accuracy</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead>
              <tbody>{Object.entries(ml.metrics.models).map(([k, m]: any) => <tr key={k} className={k === ml.metrics.deployed_model ? "selected" : ""}>
                <td>{k}</td><td>{m.accuracy}</td><td>{m.precision_macro}</td><td>{m.recall_macro}</td><td>{m.f1_macro}</td></tr>)}</tbody></table>
            <div className="muted small">Confusion matrix (deployed RandomForest, rows = true, cols = predicted)</div>
            <table className="table compact cm"><thead><tr><th />{rf.confusion_matrix.labels.map((l: string) => <th key={l}>{l}</th>)}</tr></thead>
              <tbody>{rf.confusion_matrix.matrix.map((row: number[], i: number) => <tr key={i}><th>{rf.confusion_matrix.labels[i]}</th>{row.map((v, j) => <td key={j} className={i === j ? "diag" : ""}>{v}</td>)}</tr>)}</tbody></table>
          </>) : <div className="muted">Model metrics unavailable</div>}
        </Panel>
      </div>
    </div>
  );
}
