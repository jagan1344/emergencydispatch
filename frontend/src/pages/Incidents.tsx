import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Panel, SeverityBadge, StatusBadge } from "../components/ui";
import { api } from "../services/api";
import { fmtMin, fmtTime } from "../services/format";
import { Incident } from "../types";
import { useLive } from "../hooks/useLive";

export default function Incidents() {
  const [list, setList] = useState<Incident[]>([]);
  const [filter, setFilter] = useState("active");
  const { incidents } = useLive();
  useEffect(() => {
    const q = filter === "active" ? "?active=true" : filter === "all" ? "?limit=500" : `?source=${filter}&limit=500`;
    api<Incident[]>(`/api/emergencies${q}`).then(setList).catch(() => undefined);
  }, [filter, incidents]);
  return (
    <div className="page">
      <div className="toolbar"><h1>Incidents</h1>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="active">Active</option><option value="all">All</option><option value="LIVE">Live</option>
          <option value="SIMULATION">Simulation</option><option value="SCENARIO">Scenario</option><option value="HISTORICAL_SEED">Historical seed (synthetic)</option>
        </select>
        <Link className="btn primary" to="/emergencies/new">+ New emergency</Link>
      </div>
      <Panel title={`${list.length} incidents`}>
        <table className="table">
          <thead><tr><th>Ref</th><th>Created</th><th>Type</th><th>ML</th><th>Rule</th><th>Severity</th><th>Priority</th><th>Status</th><th>Ambulance</th><th>Hospital</th><th>Response</th><th>Source</th></tr></thead>
          <tbody>{list.map((i) => (
            <tr key={i.id}>
              <td><Link to={`/emergencies/${i.id}`}>{i.reference}</Link></td><td>{fmtTime(i.created_at)}</td><td>{i.emergency_type}</td>
              <td>{i.predicted_severity || i.ml_status}</td><td>{i.rule_score?.toFixed(0)}</td><td><SeverityBadge s={i.severity} /></td>
              <td>{i.priority?.toFixed(1) ?? "—"}</td><td><StatusBadge s={i.status} /></td><td>{i.assigned_ambulance || "—"}</td>
              <td>{i.destination_hospital || "—"}</td><td>{fmtMin(i.response_time_s)}</td><td className="muted small">{i.source}</td>
            </tr>))}</tbody>
        </table>
      </Panel>
    </div>
  );
}
