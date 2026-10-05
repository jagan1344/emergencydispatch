import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useLive } from "../hooks/useLive";
import OpsMap, { MapLegend } from "../map/OpsMap";
import { Kpi, Panel, SeverityBadge, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { describeEvent, fmtMin, fmtTime } from "../services/format";

export default function Dashboard() {
  const { incidents, ambulances, events, hospitals } = useLive();
  const [summary, setSummary] = useState<any>(null);
  useEffect(() => {
    const load = () => api("/api/analytics/summary").then(setSummary).catch(() => undefined);
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, []);
  const queue = [...incidents].sort((a, b) => (a.status === "WAITING" ? 0 : 1) - (b.status === "WAITING" ? 0 : 1) || (b.priority || 0) - (a.priority || 0));
  const amb = Object.values(ambulances);
  const feed = events.map((e) => ({ e, text: describeEvent(e) })).filter((x) => x.text).slice(0, 40);
  return (
    <div className="dashboard">
      <div className="kpis">
        <Kpi testId="kpi-active" label="Active emergencies" value={summary?.active_emergencies ?? "—"} sub={`${summary?.waiting_emergencies ?? 0} waiting`} tone="warn" />
        <Kpi label="Critical (active)" value={summary?.critical_active ?? "—"} sub={`${summary?.critical_today ?? 0} critical today`} tone="crit" />
        <Kpi testId="kpi-available" label="Available ambulances" value={summary?.available_ambulances ?? "—"} sub={`of ${amb.filter((a) => a.status !== "OFFLINE").length} on duty`} tone="ok" />
        <Kpi label="In transit" value={summary?.ambulances_in_transit ?? "—"} sub={`utilization ${summary?.fleet_utilization_pct ?? 0}%`} />
        <Kpi label="Hospitals" value={summary?.hospitals ?? hospitals.length} sub={`avg load ${summary?.avg_hospital_load_pct ?? "—"}%`} />
        <Kpi label="Avg response time" value={fmtMin(summary?.avg_response_time_s)} sub={`${summary?.response_samples ?? 0} live incidents`} />
        <Kpi label="Avg dispatch time" value={fmtMin(summary?.avg_dispatch_time_s)} sub={`decision ${summary?.avg_decision_ms ?? "—"} ms`} />
        <Kpi label="Incidents today" value={summary?.incidents_today ?? "—"} sub={`${summary?.reroutes ?? 0} re-routes`} />
      </div>
      <div className="dash-grid">
        <Panel title="Live operations map" className="map-panel" actions={<Link to="/emergencies/new" className="btn primary">+ New emergency</Link>}>
          <OpsMap height="100%" />
          <MapLegend />
        </Panel>
        <div className="side">
          <Panel title={`Priority queue (${queue.length})`}>
            <table className="table compact" data-testid="priority-queue">
              <thead><tr><th>Ref</th><th>Sev</th><th>Prio</th><th>Status</th><th>Unit</th></tr></thead>
              <tbody>
                {queue.slice(0, 15).map((i) => (
                  <tr key={i.id}>
                    <td><Link to={`/emergencies/${i.id}`}>{i.reference}</Link><div className="muted small">{i.emergency_type}</div></td>
                    <td><SeverityBadge s={i.severity} /></td>
                    <td>{i.priority?.toFixed(0)}</td>
                    <td><StatusBadge s={i.status} /></td>
                    <td>{i.assigned_ambulance || "—"}{i.assigned_ambulance && ambulances[i.assigned_ambulance]?.eta_remaining_s != null &&
                      <div className="muted small">ETA {fmtMin(ambulances[i.assigned_ambulance]?.eta_remaining_s)}</div>}</td>
                  </tr>
                ))}
                {!queue.length && <tr><td colSpan={5} className="muted">No active emergencies</td></tr>}
              </tbody>
            </table>
          </Panel>
          <Panel title="Live event feed">
            <ul className="feed" data-testid="event-feed">
              {feed.map(({ e, text }, k) => (
                <li key={k} className={`ev-${e.type.toLowerCase()}`}><span className="muted">{fmtTime(new Date(e.receivedAt || Date.now()).toISOString())}</span> {text}</li>
              ))}
              {!feed.length && <li className="muted">Waiting for events…</li>}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}
