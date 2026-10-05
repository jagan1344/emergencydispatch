export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export interface Ambulance {
  id: string; call_sign: string; latitude: number; longitude: number; status: string;
  equipment_level: "BASIC" | "ADVANCED" | "ICU"; driver_status: string; current_incident: string | null;
  fuel_level: number; current_speed: number; destination: string | null; missions_today: number;
  capacity: number; last_updated: string; eta_remaining_s?: number | null;
  progress_m?: number | null; route_length_m?: number | null; leg?: string | null;
}

export interface Hospital {
  id: string; name: string; latitude: number; longitude: number; emergency_capacity: number;
  icu_available: number; trauma_available: boolean; cardiac_available: boolean; stroke_available: boolean;
  current_load: number; status: string; load_pct: number;
}

export interface Candidate {
  ambulance_id: string; score: number; components: Record<string, number>; capability_match: number;
  eta_s: number; distance_m: number; traffic_delay_s: number; equipment_level: string; suitable: boolean;
  engine?: string; fuel_level?: number; missions_today?: number; straight_line_m?: number;
}

export interface HospitalCandidate {
  hospital_id: string; name: string; score: number; components: Record<string, number>; eta_s: number;
  distance_m: number; missing_capabilities: string[]; full: boolean;
}

export interface Route {
  id: string; incident_id: string | null; ambulance_id: string | null; leg: string; engine: string;
  network_source: string; distance_m: number; completed_at: string | null; base_duration_s: number; adjusted_duration_s: number;
  osrm_duration_s: number | null; shortest_distance_m: number | null; route_efficiency: number | null;
  traffic_delay_s: number; active: boolean; created_at: string; reroute_of: string | null;
  reroute_reason: string | null; old_eta_s: number | null; time_saved_s: number | null;
  alternatives: { engine: string; distance_m: number; adjusted_duration_s: number | null; selected: boolean; feasible: boolean }[] | null;
  geometry: [number, number][]; progress_m?: number; eta_remaining_s?: number;
}

export interface Incident {
  id: string; reference: string; created_at: string; latitude: number; longitude: number; address: string | null;
  emergency_type: string; patient_age: number; heart_rate: number; respiratory_rate: number;
  oxygen_saturation: number | null; consciousness: string; bleeding: string; injury_severity: string;
  accident_type: string; breathing_difficulty: boolean; chest_pain: boolean; notes: string | null;
  rule_score: number | null; rule_severity: Severity | null; rule_components: Record<string, number> | null;
  predicted_severity: Severity | null; ml_confidence: number | null; ml_status: string; severity: Severity | null;
  severity_reasons: string[] | null; priority: number | null; priority_components: Record<string, number | null> | null;
  required_capability: string | null; assigned_ambulance: string | null; destination_hospital: string | null;
  status: string; dispatched_at: string | null; arrived_at: string | null; completed_at: string | null;
  source: string; response_time_s: number | null;
}

export interface IncidentDetail extends Incident {
  dispatch: null | {
    id: string; ambulance_id: string; method: string; score: number; eta_to_patient_s: number;
    distance_to_patient_m: number; candidates: Candidate[]; explanation: string; decision_ms: number | null;
    hospital_id: string | null; hospital_candidates: HospitalCandidate[] | null; hospital_explanation: string | null;
    status: string;
  };
  routes: Route[];
  ml_prediction: null | { model_name: string; model_version: string; predicted_class: string;
    probabilities: Record<string, number>; latency_ms: number };
  timeline: { type: string; at: string; data: Record<string, unknown> }[];
  live_eta_s: number | null;
  dispatch_error?: string;
}

export interface Road {
  road_id: string; name: string; highway_type: string; speed_limit_kph: number; current_speed_kph: number;
  congestion_level: string; blocked: boolean; incident_multiplier: number; length_m: number;
  accident: boolean; geometry?: [number, number][]; distance_m?: number;
}

export interface WsEvent { type: string; data: any; receivedAt?: number }

export interface Health {
  status: string; database: { ok: boolean }; routing: { mode: string; graph: any; osrm: any; error: string | null };
  ml_model: { available: boolean; version: string | null; error: string | null };
  mqtt: { connected: boolean }; simulator: { ambulance_sim_connected: boolean; traffic_sim_connected: boolean };
  city: { name: string; lat: number; lon: number; radius_m: number }; sim_time_scale: number;
}
