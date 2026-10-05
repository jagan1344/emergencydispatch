import { Bar, Panel } from "../components/ui";
import { useLive } from "../hooks/useLive";

const yes = (b: boolean) => (b ? "✓" : "—");

export default function Hospitals() {
  const { hospitals } = useLive();
  return (
    <div className="page">
      <div className="toolbar"><h1>Hospitals</h1><span className="muted">OSM = real location from OpenStreetMap, capabilities only from OSM tags · VERIFIED = edited via hospitals CSV · SYNTHETIC = demo template</span></div>
      <Panel title="Receiving facilities">
        <table className="table" data-testid="hospital-table">
          <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Load</th><th>Capacity</th><th>ICU beds</th><th>Trauma</th><th>Cardiac</th><th>Stroke</th><th>Data source</th></tr></thead>
          <tbody>{hospitals.map((h) => (
            <tr key={h.id}>
              <td>{h.id}</td><td>{h.name}</td><td>{h.status}</td>
              <td style={{ minWidth: 140 }}><Bar value={h.current_load} max={h.emergency_capacity} color={h.load_pct > 85 ? "var(--sev-critical)" : undefined} /> {h.current_load}/{h.emergency_capacity} ({h.load_pct}%)</td>
              <td>{h.emergency_capacity}</td><td>{h.icu_available}</td><td>{yes(h.trauma_available)}</td><td>{yes(h.cardiac_available)}</td><td>{yes(h.stroke_available)}</td>
              <td className={h.data_source === "VERIFIED" ? "" : "muted small"}>{h.data_source}{h.data_source === "OSM" && " (capabilities unverified)"}</td>
            </tr>))}</tbody>
        </table>
      </Panel>
    </div>
  );
}
