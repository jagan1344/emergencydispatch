import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Bar, ErrorNote, Panel, SeverityBadge, StatusBadge } from "../components/ui";
import { useLive } from "../hooks/useLive";
import OpsMap from "../map/OpsMap";
import { api, getUser } from "../services/api";
import { fmtKm, fmtMin, fmtTime, pct } from "../services/format";
import { IncidentDetail } from "../types";

const COMP_LABEL: Record<string, string> = { eta: "ETA", capability: "Capability mismatch", traffic: "Traffic delay",
  workload: "Workload", fuel: "Fuel", distance: "Distance", load: "Capacity load" };
const WEIGHTS: Record<string, number> = { eta: 0.4, capability: 0.2, traffic: 0.15, workload: 0.1, fuel: 0.1, distance: 0.05 };
const H_WEIGHTS: Record<string, number> = { eta: 0.45, capability: 0.25, load: 0.15, traffic: 0.15 };

export default function EmergencyDetails() {
  const { id } = useParams();
  const { subscribe, ambulances } = useLive();
  const [d, setD] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canAct = getUser()?.role !== "VIEWER";
  const load = useCallback(() => api<IncidentDetail>(`/api/emergencies/${id}`).then(setD).catch((e) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => subscribe((e) => {
    const iid = e.data?.incident_id;
    if (iid && iid === d?.id && e.type !== "AMBULANCE_LOCATION_UPDATED") window.setTimeout(load, 150);
  }), [subscribe, d?.id, load]);
  const act = async (path: string) => {
    setBusy(true); setError(null);
    try { await api(`/api/emergencies/${id}/${path}`, { method: "POST" }); await load(); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  if (!d) return <div className="page"><ErrorNote error={error} />Loading…</div>;
  const amb = d.assigned_ambulance ? ambulances[d.assigned_ambulance] : undefined;
  const liveEta = amb?.eta_remaining_s ?? d.live_eta_s;
  const active = d.routes.find((r) => r.active);
  const history = d.routes.filter((r) => !r.active);
  return (
    <div className="page details">
      <div className="toolbar">
        <h1>{d.reference} <SeverityBadge s={d.severity} /> <StatusBadge s={d.status} /></h1>
        <span className="muted">{d.emergency_type} · created {fmtTime(d.created_at)} · source {d.source}</span>
        <div className="spacer" />
        {canAct && d.status === "WAITING" && <button className="primary" disabled={busy} onClick={() => act("dispatch")} data-testid="dispatch-btn">Dispatch now</button>}
        {canAct && d.assigned_ambulance && ["DISPATCHED", "EN_ROUTE", "TO_HOSPITAL"].includes(d.status) &&
          <button disabled={busy} onClick={() => act("reroute")}>Re-evaluate route</button>}
        {canAct && !["COMPLETED", "CANCELLED"].includes(d.status) && <button className="danger" disabled={busy} onClick={() => act("cancel")}>Cancel</button>}
      </div>
      <ErrorNote error={error || d.dispatch_error || null} />
      <div className="details-grid">
        <Panel title="Severity assessment">
          <div className="sev-row">
            <div><div className="muted small">ML prediction ({d.ml_prediction?.model_name || "model"})</div>
              <div className="big" data-testid="ml-severity">{d.predicted_severity || (d.ml_status === "UNAVAILABLE" ? "MODEL UNAVAILABLE" : "—")}</div>
              <div className="muted small">confidence {pct(d.ml_confidence)}</div></div>
            <div><div className="muted small">Rule-based score</div><div className="big">{d.rule_score?.toFixed(1)}</div>
              <div className="muted small">{d.rule_severity}</div></div>
            <div><div className="muted small">Final severity</div><div className="big"><SeverityBadge s={d.severity} /></div>
              <div className="muted small">needs {d.required_capability}</div></div>
            <div><div className="muted small">Priority</div><div className="big">{d.priority?.toFixed(1)}</div><div className="muted small">/ 100</div></div>
          </div>
          {d.ml_prediction && (
            <div className="probs">{Object.entries(d.ml_prediction.probabilities).map(([k, v]) => (
              <div key={k}><span>{k}</span><Bar value={v} /><span>{pct(v)}</span></div>))}</div>
          )}
          <h3>Reasons</h3>
          <ul className="reasons">{(d.severity_reasons || []).map((r) => <li key={r}>{r}</li>)}</ul>
          {d.rule_components && <div className="muted small">Rule components: {Object.entries(d.rule_components).map(([k, v]) => `${k} ${v}`).join(" · ")}</div>}
          {d.priority_components && <div className="muted small">Priority components: {Object.entries(d.priority_components).map(([k, v]) => `${k} ${v ?? "—"}`).join(" · ")}</div>}
          <p className="disclaimer">Synthetic-data model for academic demonstration; not a medical diagnosis.</p>
        </Panel>
        <Panel title="Live status" className="map-panel">
          <div className="live-row">
            <div><span className="muted small">Ambulance</span><b data-testid="assigned-ambulance">{d.assigned_ambulance || "—"}</b></div>
            <div><span className="muted small">Live ETA</span><b data-testid="live-eta">{fmtMin(liveEta)}</b></div>
            <div><span className="muted small">Speed</span><b>{amb ? `${amb.current_speed.toFixed(0)} km/h` : "—"}</b></div>
            <div><span className="muted small">Hospital</span><b>{d.destination_hospital || "—"}</b></div>
            <div><span className="muted small">Response</span><b>{fmtMin(d.response_time_s)}</b></div>
          </div>
          <OpsMap height="340px" focus={[d.latitude, d.longitude]} highlightIncident={d.id}
            extraRoutes={history.map((r) => ({ geometry: r.geometry, color: "#8b949e", dashed: true, label: `superseded route (${r.leg})` }))} />
        </Panel>
        {d.dispatch && (
          <Panel title={`Why was ${d.dispatch.ambulance_id} selected?`} className="wide">
            <pre className="explain" data-testid="dispatch-explanation">{d.dispatch.explanation}</pre>
            <div className="muted small">Method {d.dispatch.method} · decision {d.dispatch.decision_ms?.toFixed(1)} ms · DispatchScore = 0.40·ETA + 0.20·Capability + 0.15·Traffic + 0.10·Workload + 0.10·Fuel + 0.05·Distance (lower is better)</div>
            <table className="table" data-testid="candidates">
              <thead><tr><th>Ambulance</th><th>Equip.</th><th>ETA</th><th>Distance</th><th>Traffic delay</th>
                {Object.keys(WEIGHTS).map((k) => <th key={k}>{COMP_LABEL[k]}</th>)}<th>Score</th></tr></thead>
              <tbody>{d.dispatch.candidates.map((c) => (
                <tr key={c.ambulance_id} className={c.ambulance_id === d.dispatch!.ambulance_id ? "selected" : !c.suitable ? "dim" : ""}>
                  <td>{c.ambulance_id}{c.ambulance_id === d.dispatch!.ambulance_id && " ✓"}</td>
                  <td>{c.equipment_level}{!c.suitable && <div className="muted small">unsuitable</div>}</td>
                  <td>{fmtMin(c.eta_s)}</td><td>{fmtKm(c.distance_m)}</td><td>{c.traffic_delay_s.toFixed(0)} s</td>
                  {Object.keys(WEIGHTS).map((k) => <td key={k} title={`weighted ${(WEIGHTS[k] * c.components[k]).toFixed(3)}`}>{c.components[k].toFixed(2)}</td>)}
                  <td><b>{c.score.toFixed(3)}</b></td>
                </tr>))}</tbody>
            </table>
          </Panel>
        )}
        {d.dispatch?.hospital_candidates && (
          <Panel title="Hospital selection" className="wide">
            <p data-testid="hospital-explanation">{d.dispatch.hospital_explanation}</p>
            <table className="table">
              <thead><tr><th>Hospital</th><th>ETA</th><th>Distance</th><th>Missing</th>{Object.keys(H_WEIGHTS).map((k) => <th key={k}>{COMP_LABEL[k]}</th>)}<th>Score</th></tr></thead>
              <tbody>{d.dispatch.hospital_candidates.map((h) => (
                <tr key={h.hospital_id} className={h.hospital_id === d.dispatch!.hospital_id ? "selected" : ""}>
                  <td>{h.name}</td><td>{fmtMin(h.eta_s)}</td><td>{fmtKm(h.distance_m)}</td>
                  <td>{h.missing_capabilities.join(", ") || "—"}{h.full && " (full)"}</td>
                  {Object.keys(H_WEIGHTS).map((k) => <td key={k}>{h.components[k].toFixed(2)}</td>)}<td><b>{h.score.toFixed(3)}</b></td>
                </tr>))}</tbody>
            </table>
          </Panel>
        )}
        <Panel title="Routes" className="wide">
          <table className="table" data-testid="routes-table">
            <thead><tr><th>Leg</th><th>Engine</th><th>Distance</th><th>Free-flow</th><th>Traffic ETA</th><th>Efficiency</th><th>Status</th><th>Re-route</th></tr></thead>
            <tbody>{d.routes.map((r) => (
              <tr key={r.id} className={r.active ? "selected" : ""}>
                <td>{r.leg}</td><td>{r.engine}{r.alternatives && <div className="muted small">{r.alternatives.length} candidates</div>}</td>
                <td>{fmtKm(r.distance_m)}</td><td>{fmtMin(r.base_duration_s)}</td><td>{fmtMin(r.adjusted_duration_s)}</td>
                <td>{r.route_efficiency != null ? pct(r.route_efficiency) : "—"}</td>
                <td>{r.active ? "ACTIVE" : r.completed_at ? "completed" : "superseded"}</td>
                <td>{r.reroute_reason ? <span className="reroute-cell">{r.reroute_reason}<br />Old ETA {r.old_eta_s != null ? fmtMin(r.old_eta_s) : "∞"} → New {fmtMin(r.adjusted_duration_s)}{r.time_saved_s != null && ` · saved ${fmtMin(r.time_saved_s)}`}</span> : "—"}</td>
              </tr>))}</tbody>
          </table>
          {active && <div className="muted small">Active route progress {((active.progress_m || 0) / active.distance_m * 100).toFixed(0)}%</div>}
        </Panel>
        <Panel title="Timeline (stored system events)" className="wide">
          <ul className="timeline">{d.timeline.map((e, k) => (
            <li key={k}><span className="muted">{fmtTime(e.at)}</span> <b>{e.type}</b> {summarize(e)}</li>))}</ul>
          <Link to="/emergencies">← all incidents</Link>
        </Panel>
      </div>
    </div>
  );
}

function summarize(e: { type: string; data: any }) {
  const d = e.data || {};
  if (e.type === "EMERGENCY_STATUS_CHANGED") return `${d.old_status} → ${d.status}`;
  if (e.type === "DISPATCH_CREATED") return `${d.ambulance_id} score ${d.score} ETA ${fmtMin(d.eta_s)}`;
  if (e.type === "ROUTE_RECALCULATED") return `${d.reason}; ${d.old_eta_s != null ? fmtMin(d.old_eta_s) : "∞"} → ${fmtMin(d.new_eta_s)}`;
  if (e.type === "ROUTE_CHECK") return `${d.decision}: ${d.reason}`;
  if (e.type === "HOSPITAL_SELECTED") return d.hospital_name;
  if (e.type === "AMBULANCE_STATUS_CHANGED") return `${d.ambulance_id} ${d.old_status} → ${d.status}`;
  if (e.type === "EMERGENCY_CLASSIFIED") return `ML ${d.predicted_severity ?? "n/a"} · rule ${d.rule_score} (${d.rule_severity}) · final ${d.severity}`;
  return "";
}
