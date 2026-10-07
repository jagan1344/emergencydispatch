import { useEffect, useState } from "react";
import { Bar, Panel } from "../components/ui";
import { useLive } from "../hooks/useLive";
import { api } from "../services/api";

const yes = (b: boolean) => (b ? "✓" : "—");

// capability status from the data source: OSM tags can confirm a capability; their absence is UNKNOWN, not "no"
function capCell(h: any, cap: string, shown: any) {
  const st: string | undefined = h.capability_status?.[cap];
  if (st === "UNKNOWN") return <span className="muted small" title="not in the data source - unverified">unknown</span>;
  return <span title={st || ""}>{shown}</span>;
}

export default function Hospitals() {
  const { hospitals } = useLive();
  const [pred, setPred] = useState<Record<string, any>>({});
  useEffect(() => {
    const load = () => api("/api/hospitals/predictions").then((r) =>
      setPred(Object.fromEntries(r.predictions.map((p: any) => [p.hospital_id, p])))).catch(() => undefined);
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [hospitals]);
  return (
    <div className="page">
      <div className="toolbar"><h1>Hospitals</h1><span className="muted">OSM = real location from OpenStreetMap, capabilities only from OSM tags · VERIFIED = edited via hospitals CSV · SYNTHETIC = demo template</span></div>
      <Panel title="Receiving facilities">
        <table className="table" data-testid="hospital-table">
          <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Load</th><th>Predicted (est.)</th><th>Est. wait</th><th>Capacity</th><th>ICU beds</th><th>Trauma</th><th>Cardiac</th><th>Stroke</th><th>Data source</th></tr></thead>
          <tbody>{hospitals.map((h) => (
            <tr key={h.id}>
              <td>{h.id}</td><td>{h.name}</td><td>{h.status}</td>
              <td style={{ minWidth: 140 }}><Bar value={h.current_load} max={h.emergency_capacity} color={h.load_pct > 85 ? "var(--sev-critical)" : undefined} /> {h.current_load}/{h.emergency_capacity} ({h.load_pct}%)</td>
              <td>{pred[h.id] ? `${pred[h.id].predicted_load_pct.toFixed(0)}% in ${pred[h.id].horizon_min} min` : "—"}
                {pred[h.id] && <div className="muted small">{pred[h.id].incoming_ambulances} incoming · {pred[h.id].method}</div>}</td>
              <td>{pred[h.id] ? `${pred[h.id].expected_wait_min.toFixed(0)} min` : "—"}</td>
              <td>{h.emergency_capacity}{h.capacity_source && h.capacity_source !== "VERIFIED" && <span className="muted small"> est.</span>}</td>
              <td>{capCell(h, "icu", String(h.icu_available))}</td><td>{capCell(h, "trauma", yes(h.trauma_available))}</td>
              <td>{capCell(h, "cardiac", yes(h.cardiac_available))}</td><td>{capCell(h, "stroke", yes(h.stroke_available))}</td>
              <td className={h.data_source === "VERIFIED" ? "" : "muted small"}>{h.data_source}{h.data_source === "OSM" && " (capabilities unverified)"}</td>
            </tr>))}</tbody>
        </table>
      </Panel>
    </div>
  );
}
