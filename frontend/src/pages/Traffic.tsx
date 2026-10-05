import { useEffect, useState } from "react";
import { CircleMarker } from "react-leaflet";
import { ErrorNote, Panel } from "../components/ui";
import { useLive } from "../hooks/useLive";
import OpsMap, { MapLegend } from "../map/OpsMap";
import { api, getUser } from "../services/api";
import { LEVEL_COLOR, fmtTime } from "../services/format";
import { Road } from "../types";

const LEVELS = ["FREE", "LIGHT", "MODERATE", "HEAVY", "SEVERE", "BLOCKED"];

export default function Traffic() {
  const { roads } = useLive();
  const [road, setRoad] = useState<Road | null>(null);
  const [click, setClick] = useState<[number, number] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const canAct = getUser()?.role !== "VIEWER";
  const loadEvents = () => api("/api/traffic/events?limit=40").then(setEvents).catch(() => undefined);
  useEffect(() => { loadEvents(); }, [roads]);

  const pick = async (lat: number, lon: number) => {
    setClick([lat, lon]); setError(null);
    try { setRoad(await api(`/api/traffic/roads/nearest?lat=${lat}&lon=${lon}`)); } catch (e: any) { setError(e.message); }
  };
  const pickById = (id: string) => setRoad(roads.find((r) => r.road_id === id) || null);
  const send = async (event_type: string, level?: string) => {
    if (!road) return;
    setError(null);
    try {
      const r = await api("/api/traffic/events", { method: "POST", body: { event_type, road_id: road.road_id, level } });
      const c = r.changes[0];
      if (c) setRoad({ ...road, ...c });
      setMsg(`${event_type} applied to ${road.road_id}`);
    } catch (e: any) { setError(e.message); }
  };
  const simulate = async () => {
    const r = await api("/api/traffic/simulate", { method: "POST", body: { steps: 1, changes_per_step: 8 } });
    setMsg(`${r.count} random traffic changes applied`);
  };
  const reset = async () => { const r = await api("/api/traffic/reset", { method: "POST" }); setMsg(`${r.cleared_roads} roads cleared`); };
  return (
    <div className="page two-col">
      <Panel title="Traffic control" actions={canAct && <><button onClick={simulate}>Random traffic step</button><button onClick={reset}>Clear all</button></>}>
        <p className="muted small">Click a road on the map (or a congested road below) to select it. Active ambulance routes are drawn in blue; affecting them triggers automatic re-routing.</p>
        {road ? (
          <div className="road-card" data-testid="selected-road">
            <div><b>{road.name}</b> <span className="mono">{road.road_id}</span> · {road.highway_type}</div>
            <div>State: <b style={{ color: LEVEL_COLOR[road.congestion_level] }}>{road.congestion_level}</b>{road.accident && " · ACCIDENT"} ·
              {" "}{road.current_speed_kph.toFixed(0)} / {road.speed_limit_kph.toFixed(0)} km/h · {road.length_m.toFixed(0)} m
              {road.distance_m != null && ` · ${road.distance_m.toFixed(0)} m from click`}</div>
            {canAct && <div className="btn-row">
              <button className="danger" onClick={() => send("ACCIDENT")} data-testid="btn-accident">Create accident</button>
              <button className="danger" onClick={() => send("BLOCK")} data-testid="btn-block">Block road</button>
              <button onClick={() => send("CONGESTION", "HEAVY")}>Heavy congestion</button>
              <button onClick={() => send("CONGESTION", "SEVERE")}>Severe congestion</button>
              <button onClick={() => send("CLEAR")} data-testid="btn-clear">Clear incident</button>
            </div>}
          </div>) : <div className="muted">No road selected.</div>}
        <ErrorNote error={error} />
        {msg && <div className="ok-note">{msg}</div>}
        <h3>Congested / blocked roads ({roads.length})</h3>
        <div className="scroll">
          <table className="table compact">
            <thead><tr><th>Road</th><th>Level</th><th>Speed</th></tr></thead>
            <tbody>{[...roads].sort((a, b) => LEVELS.indexOf(b.congestion_level) - LEVELS.indexOf(a.congestion_level) || +b.accident - +a.accident).slice(0, 100).map((r) => (
              <tr key={r.road_id} onClick={() => pickById(r.road_id)} className="clickable">
                <td>{r.name}<div className="muted small mono">{r.road_id}</div></td>
                <td style={{ color: LEVEL_COLOR[r.congestion_level] }}>{r.congestion_level}{r.accident && " ⚠"}</td>
                <td>{r.current_speed_kph.toFixed(0)}/{r.speed_limit_kph.toFixed(0)}</td>
              </tr>))}</tbody>
          </table>
        </div>
        <h3>Recent traffic events</h3>
        <ul className="feed small">{events.map((e) => <li key={e.id}><span className="muted">{fmtTime(e.created_at)}</span> {e.road_id} {e.event_type} {e.old_level}→{e.new_level} <span className="muted">({e.source})</span></li>)}</ul>
      </Panel>
      <Panel title="Road network" className="map-panel">
        <OpsMap height="100%" onMapClick={pick} selectedRoad={road?.road_id} onRoadClick={pickById}
          extraRoutes={road?.geometry ? [{ geometry: road.geometry, color: "#ffffff", label: "selected road" }] : []}
          extra={click ? <CircleMarker center={click} radius={5} pathOptions={{ color: "#fff" }} /> : null} />
        <MapLegend />
      </Panel>
    </div>
  );
}
