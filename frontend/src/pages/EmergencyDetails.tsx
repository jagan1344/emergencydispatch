import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Bar, ErrorNote, Panel, SeverityBadge, StatusBadge } from "../components/ui";
import { useLive } from "../hooks/useLive";
import OpsMap from "../map/OpsMap";
import { api, getUser } from "../services/api";
import { fmtKm, fmtMin, fmtTime, pct, sourceLabel } from "../services/format";
import { IncidentDetail } from "../types";
import { AIDecisionPanel, AmbulanceDecisionPanel, ConflictPanel, DecisionSummaryPanel, DecisionTrace, HospitalDecisionPanel, ReroutePanel, TrafficPanel } from "../components/DecisionPanels";


export default function EmergencyDetails() {
  const { id } = useParams();
  const { subscribe, ambulances, health } = useLive();
  const [d, setD] = useState<IncidentDetail | null>(null);
  const [dec, setDec] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canAct = getUser()?.role !== "VIEWER";
  const load = useCallback(() => Promise.all([
    api<IncidentDetail>(`/api/emergencies/${id}`).then(setD),
    api(`/api/emergencies/${id}/decision`).then(setDec),
  ]).catch((e) => setError(e.message)), [id]);
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
  const review = async (severity?: string) => {
    setBusy(true); setError(null);
    try { await api(`/api/emergencies/${id}/review`, { method: "POST", body: { severity: severity ?? null } }); await load(); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const conflictAct = async (cid: string, action: string) => {
    setError(null);
    try { await api(`/api/dispatch/conflicts/${cid}/${action}`, { method: "POST" }); await load(); }
    catch (e: any) { setError(e.message); }
  };
  if (!d || !dec) return <div className="page"><ErrorNote error={error} />Loading…</div>;
  const amb = d.assigned_ambulance ? ambulances[d.assigned_ambulance] : undefined;
  const liveEta = amb?.eta_remaining_s ?? d.live_eta_s;
  const active = d.routes.find((r) => r.active);
  const history = d.routes.filter((r) => !r.active);
  return (
    <div className="page details">
      <div className="toolbar">
        <h1>{d.reference} <SeverityBadge s={d.severity} /> <StatusBadge s={d.status} /></h1>
        <span className="muted">{d.emergency_type} · created {fmtTime(d.created_at)} · source {sourceLabel(d.source)}</span>
        <div className="spacer" />
        {canAct && d.status === "WAITING" && <button className="primary" disabled={busy} onClick={() => act("dispatch")} data-testid="dispatch-btn">Dispatch now</button>}
        {canAct && d.assigned_ambulance && ["DISPATCHED", "EN_ROUTE", "TO_HOSPITAL"].includes(d.status) &&
          <button disabled={busy} onClick={() => act("reroute")}>Re-evaluate route</button>}
        {canAct && !["COMPLETED", "CANCELLED"].includes(d.status) && <button className="danger" disabled={busy} onClick={() => act("cancel")}>Cancel</button>}
      </div>
      <ErrorNote error={error || d.dispatch_error || null} />
      {d.status === "WAITING" && (
        <div className="warn-note" role="status" data-testid="waiting-reason">
          <b>Why is this incident waiting?</b> {d.dispatch_note || "the dispatcher evaluates the queue every few seconds…"}
        </div>)}
      {active?.status === "UNAVAILABLE" && (
        <div className="error-note" role="alert" data-testid="route-unavailable">
          <b>ROUTE UNAVAILABLE — dispatcher action required</b> · incident {d.reference} · ambulance {active.ambulance_id}
          {" "}· {active.unavailable_reason || dec.summary?.route_unavailable?.reason || "no drivable route"}
          {(dec.summary?.route_unavailable?.blocked_roads || []).length > 0 &&
            <> · blocked roads {dec.summary.route_unavailable.blocked_roads.slice(0, 5).join(", ")}</>}
          <div className="small">The ambulance holds position; no walking-speed or straight-line fallback is used. Clear the closure, re-evaluate the route or reassign.</div>
        </div>)}
      {amb && amb.gps_source !== "DEVICE" && health && !health.simulator.ambulance_sim_connected &&
        ["DISPATCHED", "EN_ROUTE", "TO_HOSPITAL"].includes(d.status) && (
        <div className="warn-note" role="status">
          <b>Ambulance simulator is not running</b>: {amb.id} will not move. Start it with
          <code> python simulator\run_simulator.py</code> (or use the Crew GPS page on a phone).
        </div>)}
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
          <div className="muted small">Vitals: HR {d.heart_rate} · RR {d.respiratory_rate} · SpO₂ {d.oxygen_saturation ?? "—"} · SBP {d.systolic_bp ?? "—"} · Temp {d.temperature_c ?? "—"}</div>
          <p className="disclaimer">Academic demonstration model; not a medical diagnosis.</p>
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
        <DecisionSummaryPanel dec={dec} />
        <AIDecisionPanel dec={dec} canAct={canAct} onReview={review} />
        <TrafficPanel dec={dec} />
        <ConflictPanel dec={dec} canAct={canAct} onAct={conflictAct} />
        {d.dispatch && <AmbulanceDecisionPanel dec={dec} legacy={d.dispatch} />}
        <ReroutePanel dec={dec} />
        <HospitalDecisionPanel dec={dec} />
        <Panel title="Routes" className="wide">
          <table className="table" data-testid="routes-table">
            <thead><tr><th>Leg</th><th>Engine</th><th>Distance</th><th>Free-flow</th><th>Traffic ETA</th><th>Efficiency</th><th>Status</th><th>Re-route</th></tr></thead>
            <tbody>{d.routes.map((r) => (
              <tr key={r.id} className={r.active ? "selected" : ""}>
                <td>{r.leg}</td><td>{r.engine}{r.alternatives && <div className="muted small">{r.alternatives.length} candidates</div>}</td>
                <td>{fmtKm(r.distance_m)}</td><td>{fmtMin(r.base_duration_s)}</td><td>{fmtMin(r.adjusted_duration_s)}{r.predicted_duration_s != null && <div className="muted small">pred. {fmtMin(r.predicted_duration_s)}</div>}</td>
                <td>{r.route_efficiency != null ? pct(r.route_efficiency) : "—"}</td>
                <td>{r.status || (r.active ? "ACTIVE" : r.completed_at ? "completed" : "superseded")}</td>
                <td>{r.reroute_reason ? <span className="reroute-cell">{r.reroute_reason}<br />Old ETA {r.old_eta_s != null ? fmtMin(r.old_eta_s) : "∞"} → New {fmtMin(r.predicted_duration_s ?? r.adjusted_duration_s)}{r.time_saved_s != null && ` · saved ${fmtMin(r.time_saved_s)}`}</span> : "—"}</td>
              </tr>))}</tbody>
          </table>
          {active && (
            <div className="route-status" data-testid="route-status">
              <div><span className="muted small">Route status</span><b className={active.status === "UNAVAILABLE" ? "st-bad" : ""}>{active.status || "ACTIVE"}</b></div>
              <div><span className="muted small">ETA remaining</span><b>{fmtMin(active.eta_remaining_s ?? liveEta)}</b></div>
              <div><span className="muted small">Remaining distance</span><b>{active.remaining_m != null ? fmtKm(active.remaining_m) : "—"}</b></div>
              <div><span className="muted small">Progress</span><b>{((active.progress_m || 0) / active.distance_m * 100).toFixed(0)}%</b></div>
              <div><span className="muted small">Reroutes</span><b>{active.reroute_count ?? 0}</b></div>
              <div><span className="muted small">Last checkpoint</span><b data-testid="route-checkpoint">{active.checkpoint
                ? `${fmtKm(active.checkpoint.progress_m)} · ${fmtTime(active.checkpoint.saved_at)}` : "none yet"}</b></div>
            </div>)}
        </Panel>
        <DecisionTrace dec={dec} />
        <Panel title="Raw event log (system_events)" className="wide">
          <details><summary className="muted small">{d.timeline.length} stored events</summary>
            <ul className="timeline">{d.timeline.map((e, k) => (
              <li key={k}><span className="muted">{fmtTime(e.at)}</span> <b>{e.type}</b> {summarize(e)}</li>))}</ul></details>
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
  if (e.type === "ROUTE_UNAVAILABLE") return `${d.reason || d.detail || ""}${d.blocked_roads?.length ? ` · blocked ${d.blocked_roads.join(", ")}` : ""}`;
  if (e.type === "ROUTE_RECOVERED" || e.type === "ROUTE_RECOVERY_REPLAN") return `${d.reason || ""} · ETA ${fmtMin(d.old_eta_s)} → ${fmtMin(d.new_eta_s)}`;
  if (e.type === "HOSPITAL_SELECTED") return d.hospital_name;
  if (e.type === "DISPATCH_PENDING") return d.reason;
  if (e.type === "AMBULANCE_STATUS_CHANGED") return `${d.ambulance_id} ${d.old_status} → ${d.status}`;
  if (e.type === "EMERGENCY_CLASSIFIED") return `ML ${d.predicted_severity ?? "n/a"} · rule ${d.rule_score} (${d.rule_severity}) · final ${d.severity}`;
  return "";
}
