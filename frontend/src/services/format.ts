export const fmtMin = (s: number | null | undefined) =>
  s == null || !isFinite(s) ? "—" : `${(s / 60).toFixed(1)} min`;
export const fmtSec = (s: number | null | undefined) =>
  s == null || !isFinite(s) ? "—" : s < 120 ? `${s.toFixed(0)} s` : `${(s / 60).toFixed(1)} min`;
export const fmtKm = (m: number | null | undefined) => (m == null ? "—" : `${(m / 1000).toFixed(2)} km`);
export const fmtTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
export const pct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);

export const SEVERITY_COLOR: Record<string, string> = {
  LOW: "var(--sev-low)", MEDIUM: "var(--sev-medium)", HIGH: "var(--sev-high)", CRITICAL: "var(--sev-critical)",
};
// literal colours for canvas-rendered map layers (CSS variables are not resolved on canvas)
export const SEVERITY_HEX: Record<string, string> = { LOW: "#2ea043", MEDIUM: "#b08800", HIGH: "#d4691e", CRITICAL: "#da3633" };
export const LEVEL_COLOR: Record<string, string> = {
  FREE: "#3fb950", LIGHT: "#9ccc65", MODERATE: "#f2cc60", HEAVY: "#f0883e", SEVERE: "#f85149", BLOCKED: "#a40e26",
};
export const AMB_STATUS_COLOR: Record<string, string> = {
  AVAILABLE: "#3fb950", DISPATCHED: "#d29922", EN_ROUTE_TO_PATIENT: "#f0883e", AT_SCENE: "#f85149",
  TRANSPORTING: "#a371f7", AT_HOSPITAL: "#58a6ff", MAINTENANCE: "#8b949e", OFFLINE: "#484f58",
};

export function describeEvent(e: { type: string; data: any }): string | null {
  const d = e.data || {};
  switch (e.type) {
    case "EMERGENCY_CREATED": return `New ${d.emergency_type} emergency ${d.reference}`;
    case "EMERGENCY_STATUS_CHANGED": return `${d.reference}: ${d.old_status} → ${d.status}`;
    case "DISPATCH_CREATED": return `${d.ambulance_id} dispatched to ${d.reference} (score ${d.score}, ETA ${fmtMin(d.eta_s)})`;
    case "ROUTE_RECALCULATED": return `ROUTE RECALCULATED ${d.ambulance_id}: ${d.reason}` +
      (d.time_saved_s != null ? ` — saved ${fmtMin(d.time_saved_s)}` : "");
    case "ROUTE_CHECK": return `Route check ${d.ambulance_id}: ${d.decision} (${d.reason})`;
    case "TRAFFIC_CHANGED": return `Traffic ${d.road_id} ${d.name ? `(${d.name}) ` : ""}${d.old_level} → ${d.congestion_level}${d.event_type === "ACCIDENT" ? " [ACCIDENT]" : ""}`;
    case "HOSPITAL_SELECTED": return `${d.reference}: hospital ${d.hospital_name} selected`;
    case "HOSPITAL_WARNING": return `⚠ ${d.reference}: ${d.warning}`;
    case "AMBULANCE_STATUS_CHANGED": return `${d.ambulance_id}: ${d.old_status ?? "—"} → ${d.status}`;
    case "INCIDENT_COMPLETED": return `${d.reference} completed (response ${fmtMin(d.response_time_s)})`;
    case "DISPATCH_PENDING": return `${d.reference} waiting: ${d.reason}`;
    default: return null;
  }
}
