import { Link } from "react-router-dom";
import { Bar, Panel, StatusBadge } from "../components/ui";
import { useLive } from "../hooks/useLive";
import { api, getUser } from "../services/api";
import { fmtMin } from "../services/format";

export default function Ambulances() {
  const { ambulances, incidents, refresh } = useLive();
  const isAdmin = getUser()?.role === "ADMIN";
  const list = Object.values(ambulances).sort((a, b) => a.id.localeCompare(b.id));
  const ref = (id: string | null) => incidents.find((i) => i.id === id);
  const setStatus = async (id: string, status: string) => {
    await api(`/api/ambulances/${id}`, { method: "PATCH", body: { status } }).catch((e) => alert(e.message));
    refresh();
  };
  return (
    <div className="page">
      <div className="toolbar"><h1>Ambulances</h1><span className="muted">{list.filter((a) => a.status === "AVAILABLE").length} available of {list.length}</span></div>
      <Panel title="Fleet">
        <table className="table" data-testid="ambulance-table">
          <thead><tr><th>Unit</th><th>Equipment</th><th>Status</th><th>Assignment</th><th>Location</th><th>Speed</th><th>ETA</th><th>Fuel</th><th>Missions today</th>{isAdmin && <th>Admin</th>}</tr></thead>
          <tbody>{list.map((a) => {
            const inc = ref(a.current_incident);
            return (
              <tr key={a.id}>
                <td><b>{a.id}</b><div className="muted small">{a.call_sign}</div></td><td>{a.equipment_level}</td><td><StatusBadge s={a.status} /></td>
                <td>{inc ? <Link to={`/emergencies/${inc.id}`}>{inc.reference}</Link> : a.current_incident ? "…" : "—"}{a.destination && <div className="muted small">→ {a.destination}</div>}</td>
                <td className="mono">{a.latitude.toFixed(5)}, {a.longitude.toFixed(5)}</td>
                <td>{a.current_speed.toFixed(0)} km/h</td><td>{fmtMin(a.eta_remaining_s)}</td>
                <td style={{ minWidth: 90 }}><Bar value={a.fuel_level} max={100} color={a.fuel_level < 25 ? "var(--sev-critical)" : undefined} /> {a.fuel_level.toFixed(0)}%</td>
                <td>{a.missions_today}</td>
                {isAdmin && <td>{!a.current_incident && (a.status === "AVAILABLE"
                  ? <button className="small" onClick={() => setStatus(a.id, "MAINTENANCE")}>Maintenance</button>
                  : <button className="small" onClick={() => setStatus(a.id, "AVAILABLE")}>Set available</button>)}</td>}
              </tr>);
          })}</tbody>
        </table>
      </Panel>
    </div>
  );
}
