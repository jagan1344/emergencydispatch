import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { useLive } from "../hooks/useLive";
import { clearSession, getUser } from "../services/api";
import { fmtMin } from "../services/format";

const NAV = [
  ["/", "Dashboard"], ["/map", "Live Map"], ["/emergencies/new", "New Emergency"], ["/emergencies", "Incidents"],
  ["/ambulances", "Ambulances"], ["/hospitals", "Hospitals"], ["/traffic", "Traffic Control"],
  ["/analytics", "Analytics"], ["/simulation", "Simulation"],
];

function Dot({ ok, label, title }: { ok: boolean; label: string; title?: string }) {
  return <span className={`dot ${ok ? "ok" : "bad"}`} title={title}>{label}</span>;
}

export default function Layout() {
  const { health, wsConnected, lastReroute } = useLive();
  const user = getUser();
  const nav = useNavigate();
  const [banner, setBanner] = useState<any>(null);
  useEffect(() => {
    if (!lastReroute) return;
    setBanner(lastReroute.data);
    const t = window.setTimeout(() => setBanner(null), 12000);
    return () => window.clearTimeout(t);
  }, [lastReroute]);
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="cross">✚</span><div><b>EMS</b> Command<br /><small>{health?.city.name || "…"}</small></div></div>
        <nav>{NAV.map(([to, label]) => <NavLink key={to} to={to} end={to === "/" || to === "/emergencies"}>{label}</NavLink>)}</nav>
        <div className="user">
          <div>{user?.username} <small>({user?.role})</small></div>
          <button className="link" onClick={() => { clearSession(); nav("/login"); }}>Sign out</button>
        </div>
      </aside>
      <main>
        <div className="statusbar">
          <Dot ok={wsConnected} label="Live feed" title="WebSocket /ws" />
          <Dot ok={!!health?.database.ok} label="Database" />
          <Dot ok={!!health?.mqtt.connected} label="MQTT" />
          <Dot ok={!!health?.simulator.ambulance_sim_connected} label="Ambulance sim" />
          <Dot ok={!!health?.simulator.traffic_sim_connected} label="Traffic sim" />
          <Dot ok={!!health?.ml_model.available} label="ML model" title={health?.ml_model.version || health?.ml_model.error || ""} />
          <Dot ok={health?.routing.mode === "osm+osrm" || health?.routing.mode === "osm-graph"}
            label={`Routing: ${health?.routing.mode || "?"}`}
            title={health?.routing.mode === "synthetic-fallback" ? "Synthetic demo network - install an OSM extract for real roads" : ""} />
          <span className="clock">time ×{health?.sim_time_scale ?? "?"}</span>
        </div>
        {banner && (
          <div className="reroute-banner" role="status" data-testid="reroute-banner">
            <b>ROUTE RECALCULATED</b> · {banner.ambulance_id} · {banner.reason}
            <span>Old ETA: {banner.old_eta_s != null ? fmtMin(banner.old_eta_s) : "∞ (blocked)"}</span>
            <span>New ETA: {fmtMin(banner.new_eta_s)}</span>
            {banner.time_saved_s != null && <span>Time saved: {fmtMin(banner.time_saved_s)}</span>}
            <button className="link" onClick={() => setBanner(null)}>dismiss</button>
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}
