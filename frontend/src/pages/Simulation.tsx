import { useEffect, useState } from "react";
import { ErrorNote, Panel } from "../components/ui";
import { useLive } from "../hooks/useLive";
import { api, getUser } from "../services/api";
import { fmtTime } from "../services/format";

export default function Simulation() {
  const { simulation, health } = useLive();
  const [status, setStatus] = useState<any>(null);
  const [p, setP] = useState({ seed: 42, ambulances: 10, hospitals: 5, incidents: 20, traffic_events: 30, duration_s: 180 });
  const [error, setError] = useState<string | null>(null);
  const canAct = getUser()?.role !== "VIEWER";
  useEffect(() => { api("/api/simulation/status").then(setStatus).catch(() => undefined); }, []);
  useEffect(() => { if (simulation) setStatus(simulation); }, [simulation]);
  const call = async (path: string, body?: any) => {
    setError(null);
    try { await api(`/api/simulation/${path}`, { method: "POST", body }); setStatus(await api("/api/simulation/status")); }
    catch (e: any) { setError(e.message); }
  };
  return (
    <div className="page two-col">
      <Panel title="Simulation mode">
        <p className="muted small">Creates a reproducible schedule of incidents and traffic events from the seed (same seed → identical events).
          Ambulance movement requires the IoT simulator process ({health?.simulator.ambulance_sim_connected ? "connected" : "NOT connected"}).</p>
        <div className="form-grid">
          {(Object.keys(p) as (keyof typeof p)[]).map((k) => (
            <label key={k}>{k.replace("_", " ")}<input type="number" value={p[k]} onChange={(e) => setP({ ...p, [k]: +e.target.value })} /></label>))}
        </div>
        {canAct && <div className="btn-row">
          <button className="primary" onClick={() => call("start", p)} data-testid="start-simulation">START SIMULATION</button>
          <button className="primary danger" onClick={() => call("demo-scenario", { seed: p.seed })} data-testid="start-demo">Run demo scenario</button>
          <button onClick={() => call("stop")}>Stop</button>
          <button onClick={() => call("reset")}>Reset operations</button>
        </div>}
        <ErrorNote error={error} />
      </Panel>
      <Panel title={`Run status: ${status?.kind ?? "idle"} · ${status?.state ?? "IDLE"}`}>
        {status && <div className="muted small">seed {status.seed ?? "—"} · elapsed {status.elapsed_s ?? 0} s · {JSON.stringify(status.counters || {})}</div>}
        {status?.error && <ErrorNote error={status.error} />}
        <ol className="steps" data-testid="sim-steps">{(status?.steps || []).map((s: any) => (
          <li key={s.n}><span className="muted">{fmtTime(s.at)}</span> <b>{s.title}</b>
            {s.details?.explanation && <pre className="explain">{s.details.explanation}</pre>}
            {s.details?.candidates && <table className="table compact"><tbody>{s.details.candidates.map((c: any) =>
              <tr key={c.ambulance_id}><td>{c.ambulance_id}</td><td>{c.equipment_level}</td><td>{(c.eta_s / 60).toFixed(1)} min</td><td>score {c.score}</td><td>{c.suitable ? "" : "unsuitable"}</td></tr>)}</tbody></table>}
          </li>))}</ol>
      </Panel>
    </div>
  );
}
