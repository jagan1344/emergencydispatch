// Decision-intelligence panels for an incident: everything is rendered from GET /api/emergencies/{id}/decision.
import { useState } from "react";
import { Bar, Panel } from "./ui";
import { fmtKm, fmtMin, fmtTime, pct } from "../services/format";

const STATUS_ICON: Record<string, string> = { ok: "✓", warn: "⚠", bad: "✗", info: "" };
const MODE_LABEL: Record<string, string> = {
  AUTO_DISPATCH: "AUTO DISPATCH", DISPATCH_WITH_REVIEW: "DISPATCH + REVIEW FLAG", HUMAN_REVIEW: "HUMAN REVIEW REQUIRED",
  HUMAN_APPROVED: "HUMAN APPROVED",
};

export const Codes = ({ codes }: { codes?: string[] | null }) => !codes?.length ? null :
  <span data-testid="rejection-codes">{codes.map((c) => <span key={c} className="code-chip">{c}</span>)}</span>;

const yes = (b: boolean) => (b ? "yes" : "no");

export function DecisionSummaryPanel({ dec }: { dec: any }) {
  const s = dec.summary;
  if (!s) return null;
  const c = s.considered;
  return (
    <Panel title="Decision summary" className="wide">
      <div className="summary-grid" data-testid="decision-summary">
        <div><h3>Considered</h3><ul className="reasons small">
          <li>severity model: {yes(c.severity_model)}</li>
          <li>ambulance candidates: {c.ambulance_candidates}{c.ambulances_without_drivable_route > 0 && ` (+${c.ambulances_without_drivable_route} with no drivable route)`}</li>
          <li>hospital candidates: {c.hospital_candidates}</li>
          <li>reallocation considered: {yes(c.reallocation_considered)}</li>
          <li>rerouting considered: {yes(c.rerouting_considered)} · reroutes applied: {c.reroutes_applied}</li>
        </ul></div>
        <div><h3>Why</h3><ul className="reasons small">
          {s.why.decision_reason && <li>{s.why.decision_reason}</li>}
          {s.why.ambulance && <li>{s.why.ambulance}</li>}
          {s.why.hospital && <li>{s.why.hospital}</li>}
        </ul></div>
        <div><h3>Constraints</h3><ul className="reasons small">{s.constraints.map((x: string) => <li key={x}>{x}</li>)}</ul>
          {s.route_status && <div className="small">Route status: <b className={s.route_status === "UNAVAILABLE" ? "st-bad" : ""}>{s.route_status}</b></div>}</div>
      </div>
    </Panel>
  );
}

export function AIDecisionPanel({ dec, canAct, onReview }: { dec: any; canAct: boolean; onReview: (sev?: string) => void }) {
  const c = dec.confidence;
  const probs = Object.entries(c.class_probabilities || {}).sort((a: any, b: any) => b[1] - a[1]) as [string, number][];
  const needsReview = c.decision_mode === "HUMAN_REVIEW" && dec.incident.status === "WAITING";
  return (
    <Panel title="AI decision" className={needsReview ? "review" : ""}>
      <div className="sev-row" data-testid="ai-decision">
        <div><div className="muted small">Severity (ML)</div><div className="big">{c.predicted_severity || "—"}</div></div>
        <div><div className="muted small" title={c.label}>Model confidence{c.calibration ? " (calibrated)" : ""}</div>
          <div className="big">{c.confidence != null ? pct(c.confidence) : "n/a"}</div>
          <div className="muted small">level {c.confidence_level}{c.raw_probability != null && c.calibration ? ` · raw ${pct(c.raw_probability)}` : ""}</div></div>
        <div><div className="muted small">Decision</div><div className={`big mode-${(c.decision_mode || "").toLowerCase()}`}>{MODE_LABEL[c.decision_mode] || c.decision_mode}</div></div>
      </div>
      <div className="small">{c.decision_reason}</div>
      <div className="muted small" data-testid="confidence-basis">{c.probability_type || "model probability"}{c.calibration ? ` (${c.calibration} calibration)` : ""} · threshold applied: {c.threshold_applied || "n/a"} · not clinical certainty</div>
      {c.calibration && <div className="muted small" data-testid="calibration-info">Calibrated confidence {pct(c.calibrated_confidence ?? c.confidence)} · raw {pct(c.raw_confidence ?? c.raw_probability)} · calibration version {c.calibration_version || "—"}</div>}
      <div className="muted small">Thresholds: auto ≥ {pct(c.thresholds.high)}, review &lt; {pct(c.thresholds.low)} · model {c.model_version}
        {c.reviewed_by && ` · reviewed by ${c.reviewed_by}`}</div>
      <div className="probs">{probs.map(([k, v]) => <div key={k}><span>{k}</span><Bar value={v} /><span>{pct(v)}</span></div>)}</div>
      {needsReview && canAct && (
        <div className="btn-row" data-testid="review-actions">
          <button className="primary" onClick={() => onReview()}>Confirm {c.predicted_severity} &amp; dispatch</button>
          {["CRITICAL", "HIGH", "MEDIUM", "LOW"].filter((s) => s !== c.predicted_severity).map((s) =>
            <button key={s} onClick={() => onReview(s)}>Set {s}</button>)}
        </div>)}
    </Panel>
  );
}

export function AmbulanceDecisionPanel({ dec, legacy }: { dec: any; legacy: any }) {
  const d = dec.dispatch;
  const [open, setOpen] = useState<string | null>(null);
  if (!d) return null;
  const x = d.explanation;
  const sel = d.candidates.find((c: any) => c.ambulance_id === d.ambulance_id);
  return (
    <Panel title={`Ambulance decision: ${d.ambulance_id}`} className="wide">
      <div className="decision-grid">
        <div>
          <table className="table compact factors" data-testid="decision-factors"><tbody>
            {(x.factors || []).map((f: any) => (
              <tr key={f.factor}><td>{f.label}</td><td>{f.display}</td><td className={`st-${f.status}`}>{STATUS_ICON[f.status]}</td></tr>))}
            <tr><td><b>Final score</b></td><td><b>{d.score.toFixed(3)}</b> <span className="muted small">(lower is better)</span></td><td /></tr>
          </tbody></table>
          <div className="muted small">Method {d.method} · mode {d.decision_mode || "—"} · {d.candidates_considered} candidates ·
            objective: {d.objective}</div>
        </div>
        <div>
          <h3>Why?</h3>
          <p data-testid="why-selected">{x.summary}</p>
          <div className="muted small">Score contributions: {Object.entries(x.contributions).map(([k, v]: any) => `${k} ${v.toFixed(3)}`).join(" · ")}</div>
          <h3>Constraints</h3>
          <ul className="reasons small">{d.constraints.map((c: string) => <li key={c}>{c}</li>)}</ul>
        </div>
      </div>
      <h3>Alternatives (counterfactuals)</h3>
      <table className="table" data-testid="alternatives">
        <thead><tr><th>Ambulance</th><th>Equip.</th><th>ETA</th><th>Δ ETA</th><th>Score</th><th>Δ score</th><th>What if?</th></tr></thead>
        <tbody>
          {sel && <tr className="selected"><td>{sel.ambulance_id} ✓</td><td>{sel.equipment_level}</td><td>{fmtMin(sel.eta_s)}</td><td>—</td>
            <td>{sel.score.toFixed(3)}</td><td>—</td><td>Selected</td></tr>}
          {x.counterfactuals.map((cf: any) => {
            const c = d.candidates.find((k: any) => k.ambulance_id === cf.ambulance_id) || {};
            return (<>
              <tr key={cf.ambulance_id} className={c.suitable === false ? "dim" : ""}>
                <td>{cf.ambulance_id}</td><td>{c.equipment_level}</td><td>{fmtMin(cf.eta_s)}</td>
                <td>{cf.eta_delta_s >= 0 ? "+" : "−"}{fmtMin(Math.abs(cf.eta_delta_s))}</td><td>{cf.score.toFixed(3)}</td>
                <td>{cf.score_delta >= 0 ? "+" : ""}{cf.score_delta.toFixed(3)}<div><Codes codes={cf.rejection_codes} /></div></td>
                <td><button className="small" onClick={() => setOpen(open === cf.ambulance_id ? null : cf.ambulance_id)}>Why not {cf.ambulance_id}?</button></td>
              </tr>
              {open === cf.ambulance_id && <tr key={cf.ambulance_id + "-x"}><td colSpan={7} className="why-not">
                <div><b>{cf.why_not}</b></div><div>{cf.outcome}</div>
                {cf.tradeoffs.length > 0 && <div className="muted small">Trade-offs: {cf.tradeoffs.join("; ")}</div>}
                <div className="muted small">Factor deltas (+ = worse): {Object.entries(cf.factor_deltas).map(([k, v]: any) => `${k} ${v >= 0 ? "+" : ""}${v.toFixed(3)}`).join(" · ")}</div>
              </td></tr>}
            </>);
          })}
          {(x.unreachable || []).map((u: any) => (
            <tr key={"u-" + u.ambulance_id} className="dim"><td>{u.ambulance_id}</td><td colSpan={5} className="small">{u.reason || "no drivable route"}</td>
              <td><Codes codes={u.rejection_codes} /></td></tr>))}
        </tbody>
      </table>
      <details><summary className="muted small">Raw decision record &amp; per-component scores</summary>
        <pre className="explain" data-testid="dispatch-explanation">{legacy.explanation}</pre>
        <table className="table" data-testid="candidates">
          <thead><tr><th>Ambulance</th><th>ETA</th><th>Distance</th><th>Traffic delay</th>
            {Object.keys(x.weights).map((k) => <th key={k}>{k}</th>)}<th>Score</th></tr></thead>
          <tbody>{d.candidates.map((c: any) => (
            <tr key={c.ambulance_id}><td>{c.ambulance_id}</td><td>{fmtMin(c.eta_s)}</td><td>{fmtKm(c.distance_m)}</td>
              <td>{c.traffic_delay_s.toFixed(0)} s</td>{Object.keys(x.weights).map((k) => <td key={k}>{c.components[k].toFixed(2)}</td>)}
              <td>{c.score.toFixed(3)}</td></tr>))}</tbody>
        </table>
      </details>
    </Panel>
  );
}

export function TrafficPanel({ dec }: { dec: any }) {
  const t = dec.traffic;
  const sel = dec.routes.filter((r: any) => r.active)[0];
  return (
    <Panel title="Traffic: current vs predicted">
      {t ? (<div data-testid="traffic-panel">
        <div className="sev-row">
          <div><div className="muted small">ETA (current traffic)</div><div className="big">{fmtMin(t.current_eta_s)}</div></div>
          <div><div className="muted small">ETA (predicted, {t.horizon_min} min horizon)</div><div className="big">{fmtMin(t.predicted_eta_s)}</div></div>
          <div><div className="muted small">ETA impact</div><div className={`big ${t.eta_impact_s > 30 ? "st-bad" : ""}`}>{t.eta_impact_s == null ? "—" : `${t.eta_impact_s >= 0 ? "+" : "−"}${fmtMin(Math.abs(t.eta_impact_s))}`}</div></div>
        </div>
        {t.roads_predicted_worse.length > 0 ? <ul className="reasons small">{t.roads_predicted_worse.map((r: any) =>
          <li key={r.road_id}>{r.name} ({r.road_id}): {r.current_level} → <b>{r.predicted_level}</b></li>)}</ul>
          : <div className="muted small">No road on the remaining route is predicted to get worse.</div>}
      </div>) : <div className="muted">No active route.</div>}
      {sel?.alternatives && <><h3>Route candidates ({sel.leg})</h3>
        <table className="table compact"><thead><tr><th>Engine</th><th>Distance</th><th>ETA now</th><th>ETA predicted</th><th /></tr></thead>
          <tbody>{sel.alternatives.map((a: any, k: number) => <tr key={k} className={a.selected ? "selected" : ""}>
            <td>{a.engine}</td><td>{fmtKm(a.distance_m)}</td><td>{fmtMin(a.adjusted_duration_s)}</td><td>{fmtMin(a.predicted_duration_s)}</td>
            <td>{a.selected ? "selected" : ""}</td></tr>)}</tbody></table></>}
    </Panel>
  );
}

export function ReroutePanel({ dec }: { dec: any }) {
  if (!dec.reroutes.length) return null;
  return (
    <Panel title={`Rerouting (${dec.reroutes.length})`} className="wide">
      {dec.reroutes.slice().reverse().map((r: any) => (
        <div key={r.id} className="reroute-card" data-testid="route-updated">
          <b>ROUTE UPDATED</b> <span className="muted small">{fmtTime(r.created_at)} · {r.leg} · {r.engine}</span>
          <div>Old ETA: <b>{r.old_eta_s != null ? fmtMin(r.old_eta_s) : "∞ (blocked)"}</b> · New ETA: <b>{fmtMin(r.predicted_duration_s ?? r.adjusted_duration_s)}</b>
            {r.time_saved_s != null && <> · Improvement: <b>{fmtMin(r.time_saved_s)}</b></>}</div>
          <div className="small">Reason: {r.reroute_reason}</div>
        </div>))}
    </Panel>
  );
}

export function HospitalDecisionPanel({ dec }: { dec: any }) {
  const h = dec.hospital;
  if (!h) return null;
  const best = h.candidates.find((c: any) => c.hospital_id === h.selected) || h.candidates[0];
  const f = best?.forecast;
  return (
    <Panel title="Hospital decision" className="wide">
      <div className="sev-row" data-testid="hospital-decision">
        <div><div className="muted small">Selected hospital</div><div className="big">{best?.name}</div></div>
        <div><div className="muted small">ETA</div><div className="big">{fmtMin(best?.eta_s)}</div></div>
        {f && <><div><div className="muted small">Current load</div><div className="big">{f.current_load}/{f.capacity}</div></div>
          <div><div className="muted small">Predicted load at arrival</div><div className="big">{f.predicted_load_pct.toFixed(0)}%</div></div>
          <div><div className="muted small">Expected wait (est.)</div><div className="big">{f.expected_wait_min.toFixed(0)} min</div></div></>}
      </div>
      <p data-testid="hospital-explanation">{h.explanation}</p>
      <table className="table">
        <thead><tr><th>Hospital</th><th>ETA</th><th>Load now</th><th>Predicted</th><th>Est. wait</th><th>Time to treatment</th><th>Missing</th><th>Score</th><th>Why not?</th></tr></thead>
        <tbody>{h.candidates.map((c: any) => {
          const cf = h.counterfactual?.counterfactuals?.find((x: any) => x.hospital_id === c.hospital_id);
          const wn = h.counterfactual?.why_not?.find((x: any) => x.hospital_id === c.hospital_id);
          const unk: string[] = c.unknown_capabilities || [];
          const lacking = c.missing_capabilities.filter((m: string) => !unk.includes(m));
          return (<tr key={c.hospital_id} className={c.hospital_id === h.selected ? "selected" : ""}>
            <td>{c.name}</td><td>{fmtMin(c.eta_s)}</td>
            <td>{c.forecast ? `${c.forecast.current_load}/${c.forecast.capacity}` : "—"}</td>
            <td>{c.forecast ? `${c.forecast.predicted_load_pct.toFixed(0)}%` : "—"}</td>
            <td>{c.forecast ? `${c.forecast.expected_wait_min.toFixed(0)} min` : "—"}</td>
            <td>{fmtMin(c.time_to_treatment_s)}</td>
            <td>{lacking.join(", ") || (unk.length ? "" : "—")}{unk.length > 0 && <span className="muted small">{lacking.length ? "; " : ""}unknown: {unk.join(", ")}</span>}</td>
            <td><b>{c.score.toFixed(3)}</b></td>
            <td className="small" data-testid="hospital-why-not">{c.hospital_id === h.selected ? "selected" : <>{wn?.reasons?.join("; ") || cf?.why_not}<div><Codes codes={wn?.rejection_codes} /></div></>}</td>
          </tr>);
        })}</tbody>
      </table>
      {f && <div className="muted small">Estimates (not actual hospital data; source {f.source || "SIMULATION"}): {f.method}, {f.data_points} observations,
        confidence {f.confidence}, mean stay {f.mean_stay_min} min, model {f.model_version}.</div>}
    </Panel>
  );
}

export function ConflictPanel({ dec, canAct, onAct }: { dec: any; canAct: boolean; onAct: (id: string, a: string) => void }) {
  if (!dec.conflicts.length) return null;
  return (
    <Panel title="Resource conflicts" className="wide">
      {dec.conflicts.map((c: any) => (
        <div key={c.id} className={`conflict-card d-${c.decision.toLowerCase()}`} data-testid="conflict">
          <b>{c.kind === "REALLOCATION" ? "RESOURCE REALLOCATION" : "RESOURCE CONFLICT"}</b> · {c.ambulance_id} · <b>{c.decision}</b>
          <span className="muted small"> {fmtTime(c.created_at)}</span>
          <div className="small">{c.reason}</div>
          <div className="muted small">
            {c.to_eta_s != null && `requester ETA ${fmtMin(c.to_eta_s)}`}{c.alternative_eta_s != null && ` (best alternative ${fmtMin(c.alternative_eta_s)})`}
            {c.impact_s != null && ` · impact on other incident ${c.impact_s >= 0 ? "+" : ""}${fmtMin(c.impact_s)}`}
            {c.resolved_by && ` · resolved by ${c.resolved_by}`}</div>
          {c.details?.candidates?.length > 0 && (
            <table className="table small" data-testid="conflict-candidates"><thead><tr><th>Unit</th><th>Committed to</th><th>Status</th><th>ETA to this call</th><th>Delay caused</th><th>Reason</th></tr></thead>
              <tbody>{c.details.candidates.map((x: any) => (
                <tr key={x.ambulance_id} className={x.selected ? "selected" : ""}><td>{x.ambulance_id}{x.selected ? " ✓" : ""}</td>
                  <td>{x.donor} ({x.donor_severity})</td><td>{x.status.replace(/_/g, " ")}</td>
                  <td>{x.to_eta_s != null ? fmtMin(x.to_eta_s) : "—"}</td><td>{x.donor_delay_s != null ? fmtMin(x.donor_delay_s) : "—"}</td>
                  <td className="muted">{x.reason}</td></tr>))}</tbody></table>)}
          {c.decision === "ESCALATED" && canAct && <div className="btn-row">
            <button className="primary" onClick={() => onAct(c.id, "approve")}>Approve reallocation</button>
            <button onClick={() => onAct(c.id, "reject")}>Reject</button></div>}
        </div>))}
    </Panel>
  );
}

export function DecisionTrace({ dec }: { dec: any }) {
  return (
    <Panel title="Decision trace" className="wide">
      <ol className="trace" data-testid="decision-trace">{dec.trace.map((t: any, k: number) => (
        <li key={k}><span className="muted">{fmtTime(t.at)}</span> <b>{t.title}</b>{t.detail && <div className="muted small">{t.detail}</div>}</li>))}</ol>
    </Panel>
  );
}
