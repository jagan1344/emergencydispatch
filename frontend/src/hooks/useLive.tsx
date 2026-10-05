// Live operational state: initial REST snapshot + incremental WebSocket updates (/ws).
import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, getToken } from "../services/api";
import { Ambulance, Health, Hospital, Incident, Road, Route, WsEvent } from "../types";

interface LiveState {
  ambulances: Record<string, Ambulance>;
  incidents: Incident[];
  routes: Route[];
  hospitals: Hospital[];
  roads: Road[];
  health: Health | null;
  events: WsEvent[];
  wsConnected: boolean;
  lastReroute: WsEvent | null;
  simulation: any;
  refresh: () => void;
  subscribe: (fn: (e: WsEvent) => void) => () => void;
}

const Ctx = createContext<LiveState | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [ambulances, setAmbulances] = useState<Record<string, Ambulance>>({});
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [routes, setRoutes] = useState<Route[]>([]);
  const [hospitals, setHospitals] = useState<Hospital[]>([]);
  const [roads, setRoads] = useState<Road[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [events, setEvents] = useState<WsEvent[]>([]);
  const [wsConnected, setWs] = useState(false);
  const [lastReroute, setLastReroute] = useState<WsEvent | null>(null);
  const [simulation, setSimulation] = useState<any>(null);
  const listeners = useRef(new Set<(e: WsEvent) => void>());
  const timers = useRef<Record<string, number>>({});

  const loaders = useMemo(() => ({
    ambulances: () => api<Ambulance[]>("/api/ambulances").then((l) => setAmbulances(Object.fromEntries(l.map((a) => [a.id, a])))),
    incidents: () => api<Incident[]>("/api/emergencies?active=true&limit=1000").then(setIncidents),
    routes: () => api<Route[]>("/api/routes?active=true").then(setRoutes),
    hospitals: () => api<Hospital[]>("/api/hospitals").then(setHospitals),
    roads: () => api<Road[]>("/api/traffic/roads?congested_only=true").then(setRoads),
    health: () => fetch("/api/health").then((r) => r.json()).then(setHealth),
  }), []);

  // coalesce bursts of events into one reload per resource
  const debounced = useCallback((key: keyof typeof loaders, ms = 400) => {
    window.clearTimeout(timers.current[key]);
    timers.current[key] = window.setTimeout(() => { loaders[key]().catch(() => undefined); }, ms);
  }, [loaders]);

  const refresh = useCallback(() => {
    Object.values(loaders).forEach((fn) => fn().catch(() => undefined));
  }, [loaders]);

  useEffect(() => {
    refresh();
    const h = window.setInterval(() => { loaders.health().catch(() => undefined); }, 5000);
    const full = window.setInterval(refresh, 30000);   // safety net in case an event was missed
    return () => { window.clearInterval(h); window.clearInterval(full); };
  }, [refresh, loaders]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws?token=${encodeURIComponent(getToken() || "")}`);
      ws.onopen = () => { setWs(true); refresh(); };
      ws.onclose = () => { setWs(false); if (!closed) retry = window.setTimeout(connect, 2000); };
      ws.onmessage = (m) => {
        const ev: WsEvent = { ...JSON.parse(m.data), receivedAt: Date.now() };
        handle(ev);
        listeners.current.forEach((fn) => fn(ev));
      };
    };
    const handle = (ev: WsEvent) => {
      const d = ev.data;
      switch (ev.type) {
        case "AMBULANCE_LOCATION_UPDATED":
          setAmbulances((prev) => prev[d.ambulance_id] ? { ...prev, [d.ambulance_id]: { ...prev[d.ambulance_id],
            latitude: d.latitude, longitude: d.longitude, current_speed: d.speed, eta_remaining_s: d.eta_remaining_s,
            progress_m: d.progress_m, route_length_m: d.route_length_m, leg: d.leg } } : prev);
          return;  // too frequent for the event feed
        case "AMBULANCE_STATUS_CHANGED":
          setAmbulances((prev) => prev[d.ambulance_id] ? { ...prev, [d.ambulance_id]: { ...prev[d.ambulance_id], status: d.status } } : prev);
          debounced("ambulances", 800);
          break;
        case "EMERGENCY_CREATED": case "EMERGENCY_STATUS_CHANGED": case "EMERGENCY_CLASSIFIED": case "EMERGENCY_PRIORITY_CHANGED":
          debounced("incidents");
          break;
        case "DISPATCH_CREATED": case "HOSPITAL_SELECTED": case "INCIDENT_COMPLETED":
          debounced("routes"); debounced("incidents");
          break;
        case "ROUTE_RECALCULATED":
          setLastReroute(ev); debounced("routes", 100);
          break;
        case "TRAFFIC_CHANGED":
          debounced("roads", 600);
          break;
        case "HOSPITAL_CAPACITY_CHANGED":
          setHospitals((prev) => prev.map((h) => (h.id === d.id ? { ...h, ...d } : h)));
          break;
        case "SIMULATION_STATUS":
          setSimulation(d);
          break;
        case "SIMULATION_RESET":
          refresh();
          break;
      }
      if (ev.type !== "EMERGENCY_PRIORITY_CHANGED") setEvents((prev) => [ev, ...prev].slice(0, 200));
    };
    connect();
    return () => { closed = true; window.clearTimeout(retry); ws?.close(); };
  }, [debounced, refresh]);

  const subscribe = useCallback((fn: (e: WsEvent) => void) => {
    listeners.current.add(fn);
    return () => { listeners.current.delete(fn); };
  }, []);

  const value = { ambulances, incidents, routes, hospitals, roads, health, events, wsConnected, lastReroute,
    simulation, refresh, subscribe };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLive(): LiveState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useLive outside LiveProvider");
  return v;
}
