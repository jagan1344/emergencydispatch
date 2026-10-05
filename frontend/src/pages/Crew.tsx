// Crew GPS: turns a phone into the ambulance's real GPS unit (browser Geolocation API).
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ErrorNote, Panel, StatusBadge } from "../components/ui";
import { useLive } from "../hooks/useLive";
import OpsMap from "../map/OpsMap";
import { api } from "../services/api";
import { fmtMin } from "../services/format";

export default function Crew() {
  const { ambulances, incidents, routes } = useLive();
  const [unit, setUnit] = useState<string>(() => { try { return localStorage.getItem("crew_unit") || ""; } catch { return ""; } });
  const [sharing, setSharing] = useState(false);
  const [last, setLast] = useState<any>(null);
  const [fix, setFix] = useState<GeolocationPosition | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(0);
  const watch = useRef<number | null>(null);
  const amb = unit ? ambulances[unit] : undefined;
  const inc = amb?.current_incident ? incidents.find((i) => i.id === amb.current_incident) : undefined;
  const route = routes.find((r) => r.ambulance_id === unit);
  const secure = window.isSecureContext;

  useEffect(() => { try { localStorage.setItem("crew_unit", unit); } catch { /* ignore */ } }, [unit]);
  useEffect(() => () => { if (watch.current != null) navigator.geolocation.clearWatch(watch.current); }, []);

  const start = () => {
    setError(null);
    if (!("geolocation" in navigator)) { setError("This browser has no GPS/geolocation support."); return; }
    const send = async (pos: GeolocationPosition) => {
      setFix(pos);
      try {
        const r = await api(`/api/ambulances/${unit}/gps`, { method: "POST", body: {
          latitude: pos.coords.latitude, longitude: pos.coords.longitude,
          speed_kph: pos.coords.speed != null && pos.coords.speed >= 0 ? pos.coords.speed * 3.6 : null,
          accuracy_m: pos.coords.accuracy } });
        setLast(r); setSent((n) => n + 1); setError(null);
      } catch (e: any) { setError(e.message); }
    };
    // transient errors (cold start, tunnel, timeout) do not stop tracking; the watch keeps running
    const onErr = (err: GeolocationPositionError) =>
      setError(`GPS ${["", "permission denied", "position unavailable", "timeout"][err.code] || "error"}${err.message ? `: ${err.message}` : ""}`);
    const opts = { enableHighAccuracy: true, maximumAge: 2000, timeout: 30000 };
    navigator.geolocation.getCurrentPosition(send, onErr, opts);   // immediate first fix
    watch.current = navigator.geolocation.watchPosition(send, onErr, opts);
    setSharing(true);
  };
  const stop = async (release: boolean) => {
    if (watch.current != null) navigator.geolocation.clearWatch(watch.current);
    watch.current = null; setSharing(false);
    if (release) await api(`/api/ambulances/${unit}/gps/release`, { method: "POST" }).catch(() => undefined);
  };
  const arrived = async () => {
    try { await api(`/api/ambulances/${unit}/arrived`, { method: "POST" }); } catch (e: any) { setError(e.message); }
  };

  return (
    <div className="page two-col">
      <Panel title="Crew GPS (real device location)">
        <p className="muted small">Open this page on the ambulance crew's phone. The phone's GPS position replaces the simulator for the
          selected unit: progress, ETA, arrival (within 40 m) and off-route re-routing are computed from the real position.</p>
        {!secure && <div className="error-note">Location needs a secure page. On a phone, start the frontend with <code>npm run dev:https</code> and
          open <code>https://&lt;laptop-IP&gt;:5173/crew</code>.</div>}
        <label>Ambulance
          <select value={unit} onChange={(e) => setUnit(e.target.value)} disabled={sharing} data-testid="crew-unit">
            <option value="">— select unit —</option>
            {Object.values(ambulances).sort((a, b) => a.id.localeCompare(b.id)).map((a) =>
              <option key={a.id} value={a.id}>{a.id} · {a.equipment_level} · {a.status}</option>)}
          </select>
        </label>
        <div className="btn-row">
          {!sharing ? <button className="primary" disabled={!unit} onClick={start} data-testid="crew-start">Start sharing GPS</button>
            : <><button onClick={() => stop(false)}>Pause</button><button onClick={() => stop(true)}>Stop &amp; hand back to simulator</button></>}
          {inc && <button className="danger" onClick={arrived}>Confirm arrival</button>}
        </div>
        <ErrorNote error={error} />
        {amb && (
          <div className="road-card">
            <div><b>{amb.id}</b> <StatusBadge s={amb.status} /> · source <b data-testid="crew-source">{last ? "DEVICE" : amb.gps_source}</b></div>
            {inc ? <div>Mission <Link to={`/emergencies/${inc.id}`}>{inc.reference}</Link> · {inc.emergency_type} · {inc.severity}
              <br />Destination: {amb.destination || "—"} · ETA <b>{fmtMin(amb.eta_remaining_s)}</b></div> : <div className="muted">No active mission.</div>}
            {fix && <div className="small">Fix {fix.coords.latitude.toFixed(5)}, {fix.coords.longitude.toFixed(5)} · ±{fix.coords.accuracy.toFixed(0)} m · {sent} sent</div>}
            {last?.off_route_m != null && <div className="small">Distance from planned route: {last.off_route_m} m{last.rerouted ? " · RE-ROUTED from your position" : ""}</div>}
          </div>
        )}
      </Panel>
      <Panel title="Navigation" className="map-panel">
        <OpsMap height="100%" focus={fix ? [fix.coords.latitude, fix.coords.longitude] : null}
          highlightIncident={inc?.id} extraRoutes={route ? [{ geometry: route.geometry, color: "#ffd33d", label: "your route" }] : []} />
      </Panel>
    </div>
  );
}
