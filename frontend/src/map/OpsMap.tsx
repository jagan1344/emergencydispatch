// Operational map: OpenStreetMap tiles (Leaflet) with ambulances, incidents, hospitals, routes and traffic.
import L from "leaflet";
import { ReactNode, useEffect, useMemo } from "react";
import { CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import { useLive } from "../hooks/useLive";
import { AMB_STATUS_COLOR, LEVEL_COLOR, SEVERITY_HEX, fmtMin } from "../services/format";
import { Link } from "react-router-dom";

export interface Layers { ambulances: boolean; incidents: boolean; hospitals: boolean; routes: boolean; traffic: boolean }
export const ALL_LAYERS: Layers = { ambulances: true, incidents: true, hospitals: true, routes: true, traffic: true };

function ambIcon(status: string, label: string, selected: boolean) {
  const c = AMB_STATUS_COLOR[status] || "#888";
  return L.divIcon({
    className: "", iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<div class="amb-marker${selected ? " sel" : ""}" style="background:${c}" data-amb="${label}" title="${label}">✚</div>`,
  });
}
const hospitalIcon = L.divIcon({ className: "", iconSize: [24, 24], iconAnchor: [12, 12],
  html: `<div class="hosp-marker">H</div>` });

function ClickHandler({ onClick }: { onClick?: (lat: number, lon: number) => void }) {
  useMapEvents({ click: (e) => onClick?.(e.latlng.lat, e.latlng.lng) });
  return null;
}

function Recenter({ center }: { center: [number, number] | null }) {
  const map = useMap();
  useEffect(() => { if (center) map.setView(center, Math.max(map.getZoom(), 14)); }, [center?.[0], center?.[1]]);  // eslint-disable-line
  return null;
}

interface Props {
  layers?: Layers; height?: string; onMapClick?: (lat: number, lon: number) => void;
  focus?: [number, number] | null; highlightIncident?: string | null; extra?: ReactNode;
  selectedRoad?: string | null; onRoadClick?: (roadId: string) => void; showAllRoutes?: boolean;
  extraRoutes?: { geometry: [number, number][]; color: string; dashed?: boolean; label?: string }[];
}

export default function OpsMap({ layers = ALL_LAYERS, height = "100%", onMapClick, focus = null, highlightIncident,
  extra, selectedRoad, onRoadClick, extraRoutes = [] }: Props) {
  const { ambulances, incidents, hospitals, routes, roads, health } = useLive();
  const center: [number, number] = health ? [health.city.lat, health.city.lon] : [12.9716, 77.5946];
  // picking mode (e.g. New Emergency): every click sets a location; markers do not open popups/links
  const picking = !!onMapClick && !onRoadClick;
  const pick = (e: L.LeafletMouseEvent) => onMapClick?.(e.latlng.lat, e.latlng.lng);
  const ambList = useMemo(() => Object.values(ambulances).filter((a) => a.status !== "OFFLINE"), [ambulances]);
  if (!health) return <div className="map-placeholder" style={{ height }}>Loading map…</div>;
  return (
    <MapContainer center={center} zoom={14} style={{ height, width: "100%" }} preferCanvas>
      <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
      <ClickHandler onClick={onMapClick} />
      <Recenter center={focus} />
      {layers.traffic && roads.map((r) => r.geometry && (
        <Polyline key={r.road_id} positions={r.geometry}
          pathOptions={{ color: LEVEL_COLOR[r.congestion_level] || "#888", weight: selectedRoad === r.road_id ? 9 : r.blocked ? 7 : 5,
            opacity: 0.85, dashArray: r.blocked ? "6 6" : undefined }}
          eventHandlers={{ click: (e) => (onRoadClick ? onRoadClick(r.road_id) : onMapClick?.(e.latlng.lat, e.latlng.lng)) }}>
          <Tooltip sticky>{r.name} ({r.road_id}) · {r.congestion_level}{r.accident ? " · ACCIDENT" : ""} · {r.current_speed_kph.toFixed(0)}/{r.speed_limit_kph.toFixed(0)} km/h</Tooltip>
        </Polyline>
      ))}
      {layers.routes && routes.map((rt) => (
        <Polyline key={rt.id} positions={rt.geometry}
          pathOptions={{ color: rt.leg === "TO_HOSPITAL" ? "#a371f7" : "#58a6ff",
            weight: highlightIncident && rt.incident_id === highlightIncident ? 7 : 5, opacity: 0.9 }}
          eventHandlers={{ click: (e) => onMapClick?.(e.latlng.lat, e.latlng.lng) }}>
          <Tooltip sticky>{rt.ambulance_id} · {rt.leg} · {rt.engine} · ETA {fmtMin(rt.eta_remaining_s ?? rt.adjusted_duration_s)}
            {rt.reroute_of ? " · RE-ROUTED" : ""}</Tooltip>
        </Polyline>
      ))}
      {extraRoutes.map((r, i) => (
        <Polyline key={`x${i}`} positions={r.geometry} pathOptions={{ color: r.color, weight: 4, dashArray: r.dashed ? "8 8" : undefined, opacity: 0.8 }}
          eventHandlers={{ click: (e) => onMapClick?.(e.latlng.lat, e.latlng.lng) }}>
          {r.label && <Tooltip sticky>{r.label}</Tooltip>}
        </Polyline>
      ))}
      {layers.hospitals && hospitals.filter((h) => h.status !== "CLOSED").map((h) => (
        <Marker key={h.id} position={[h.latitude, h.longitude]} icon={hospitalIcon} eventHandlers={picking ? { click: pick } : {}}>
          {!picking && <Popup><b>{h.name}</b><br />Load {h.current_load}/{h.emergency_capacity} · ICU beds {h.icu_available}<br />
            {[h.trauma_available && "Trauma", h.cardiac_available && "Cardiac", h.stroke_available && "Stroke"].filter(Boolean).join(" · ") || "General"}</Popup>}
        </Marker>
      ))}
      {layers.incidents && incidents.map((i) => (
        <CircleMarker key={i.id} center={[i.latitude, i.longitude]} radius={highlightIncident === i.id ? 12 : 9}
          pathOptions={{ color: "#fff", weight: 2, fillColor: SEVERITY_HEX[i.severity || "LOW"], fillOpacity: 0.95 }}
          eventHandlers={picking ? { click: pick } : {}}>
          {!picking && <Popup><b>{i.reference}</b> · {i.emergency_type}<br />Severity {i.severity} · Priority {i.priority?.toFixed(1)}<br />
            Status {i.status}<br /><Link to={`/emergencies/${i.id}`}>Open details →</Link></Popup>}
        </CircleMarker>
      ))}
      {layers.ambulances && ambList.map((a) => (
        <Marker key={a.id} position={[a.latitude, a.longitude]} icon={ambIcon(a.status, a.id, false)} zIndexOffset={1000}
          eventHandlers={picking ? { click: pick } : {}}>
          {!picking && <Popup><b>{a.id}</b> ({a.equipment_level})<br />{a.status} · {a.current_speed.toFixed(0)} km/h<br />
            Fuel {a.fuel_level.toFixed(0)}%{a.eta_remaining_s != null ? <><br />ETA {fmtMin(a.eta_remaining_s)}</> : null}</Popup>}
        </Marker>
      ))}
      {extra}
    </MapContainer>
  );
}

export function MapLegend() {
  return (
    <div className="legend">
      {Object.entries(AMB_STATUS_COLOR).filter(([k]) => k !== "OFFLINE").map(([k, c]) => (
        <span key={k}><i style={{ background: c }} />{k.replace(/_/g, " ").toLowerCase()}</span>))}
      <span className="sep" />
      {Object.entries(LEVEL_COLOR).map(([k, c]) => <span key={k}><i className="line" style={{ background: c }} />{k.toLowerCase()}</span>)}
    </div>
  );
}
