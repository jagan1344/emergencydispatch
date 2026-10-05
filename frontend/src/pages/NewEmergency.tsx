import { FormEvent, useState } from "react";
import { CircleMarker } from "react-leaflet";
import { useNavigate } from "react-router-dom";
import { Panel } from "../components/ui";
import { useLive } from "../hooks/useLive";
import OpsMap from "../map/OpsMap";
import { api } from "../services/api";

const TYPES = ["accident", "cardiac", "respiratory", "trauma", "fire", "stroke", "other"];
const GRADES = ["NONE", "MINOR", "MODERATE", "SEVERE"];

export default function NewEmergency() {
  const { health } = useLive();
  const nav = useNavigate();
  const [loc, setLoc] = useState<[number, number] | null>(null);
  const [f, setF] = useState({
    emergency_type: "accident", patient_age: 45, heart_rate: 110, respiratory_rate: 24, oxygen_saturation: 93,
    consciousness: "ALERT", bleeding: "MODERATE", injury_severity: "MODERATE", accident_type: "ROAD",
    breathing_difficulty: false, chest_pain: false, address: "", notes: "",
  });
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: any) => setF((p) => ({ ...p, [k]: v }));
  const location = loc || (health ? [health.city.lat, health.city.lon] as [number, number] : null);
  const caseBody = () => ({ ...f, patient_age: +f.patient_age, heart_rate: +f.heart_rate, respiratory_rate: +f.respiratory_rate,
    oxygen_saturation: f.oxygen_saturation ? +f.oxygen_saturation : null });

  const doPreview = async () => {
    setError(null);
    try { setPreview(await api("/api/ml/predict", { method: "POST", body: caseBody() })); }
    catch (e: any) { setError(e.message); }
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!location) return;
    setBusy(true); setError(null);
    try {
      const inc = await api("/api/emergencies", { method: "POST", body: { ...caseBody(), latitude: location[0], longitude: location[1],
        address: f.address || null, notes: f.notes || null } });
      nav(`/emergencies/${inc.id}`);
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };
  const num = (k: keyof typeof f, label: string, min: number, max: number) => (
    <label>{label}<input type="number" name={k} min={min} max={max} value={f[k] as number} onChange={(e) => set(k, e.target.value)} required /></label>
  );
  const sel = (k: keyof typeof f, label: string, opts: string[]) => (
    <label>{label}<select name={k} value={f[k] as string} onChange={(e) => set(k, e.target.value)}>{opts.map((o) => <option key={o}>{o}</option>)}</select></label>
  );
  return (
    <div className="page two-col">
      <Panel title="Create emergency">
        <form onSubmit={submit} className="form-grid" data-testid="emergency-form">
          {sel("emergency_type", "Emergency type", TYPES)}
          {sel("accident_type", "Accident type", ["NONE", "ROAD", "FALL", "FIRE", "INDUSTRIAL", "OTHER"])}
          {num("patient_age", "Age", 0, 120)}
          {num("heart_rate", "Heart rate (bpm)", 0, 300)}
          {num("respiratory_rate", "Respiratory rate (/min)", 0, 80)}
          {num("oxygen_saturation", "SpO₂ (%)", 50, 100)}
          {sel("consciousness", "Consciousness (AVPU)", ["ALERT", "VERBAL", "PAIN", "UNRESPONSIVE"])}
          {sel("bleeding", "Bleeding", GRADES)}
          {sel("injury_severity", "Injury severity", GRADES)}
          <label className="check"><input type="checkbox" name="breathing_difficulty" checked={f.breathing_difficulty} onChange={(e) => set("breathing_difficulty", e.target.checked)} /> Breathing difficulty</label>
          <label className="check"><input type="checkbox" name="chest_pain" checked={f.chest_pain} onChange={(e) => set("chest_pain", e.target.checked)} /> Chest pain</label>
          <label className="wide">Address / landmark<input name="address" value={f.address} onChange={(e) => set("address", e.target.value)} /></label>
          <label className="wide">Location<input readOnly data-testid="location" value={location ? `${location[0].toFixed(5)}, ${location[1].toFixed(5)}` : ""} /></label>
          <p className="muted small wide">Click on the map to set the incident location.</p>
          {error && <div className="error-note wide" role="alert">{error}</div>}
          <div className="wide actions">
            <button type="button" onClick={doPreview}>Preview severity</button>
            <button className="primary danger" disabled={busy || !location} data-testid="create-emergency">{busy ? "Creating…" : "CREATE EMERGENCY"}</button>
          </div>
        </form>
        {preview && (
          <div className="preview">
            <div><b>ML prediction:</b> {preview.ml.severity} ({(preview.ml.confidence * 100).toFixed(0)}%) · <b>Rule score:</b> {preview.rule.score} ({preview.rule.severity}) · <b>Final:</b> {preview.final_severity}</div>
            <div className="muted small">{preview.rule.reasons.join(" · ") || "no risk factors"}</div>
          </div>
        )}
        <p className="disclaimer">Severity model trained on synthetic data for academic demonstration only — not a medical diagnosis.</p>
      </Panel>
      <Panel title="Incident location" className="map-panel">
        <OpsMap height="100%" onMapClick={(lat, lon) => setLoc([lat, lon])}
          extra={location ? <CircleMarker center={location} radius={11} pathOptions={{ color: "#fff", fillColor: "#f85149", fillOpacity: 1 }} /> : null} />
      </Panel>
    </div>
  );
}
