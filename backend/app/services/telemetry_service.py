"""IoT ingestion: location / telemetry messages are applied in memory immediately (WebSocket push) and
written to PostGIS in batches every second, so 100 ambulances × 1 Hz cost ~1 transaction per second."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from sqlalchemy import text

from app.services.events import broadcast_now
from app.services.routes_service import ACTIVE, remaining_eta
from app.utils.timeutil import utcnow

log = logging.getLogger("app.telemetry")


class TelemetryBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self.locations: dict[str, dict] = {}     # latest fix per ambulance (ambulances table)
        self.history: list[dict] = []            # every fix (ambulance_locations table)
        self.iot: list[dict] = []                # every MQTT message (iot_messages table)
        self.fuel: dict[str, float] = {}
        self.latest: dict[str, dict] = {}        # read model for API (latest position, speed, eta)

    def record_iot(self, topic: str, payload: dict) -> None:
        with self._lock:
            self.iot.append({"topic": topic[:128], "payload": json.dumps(payload, default=str)})

    def handle_location(self, ambulance_id: str, p: dict) -> None:
        try:
            lat, lon = float(p["latitude"]), float(p["longitude"])
        except (KeyError, TypeError, ValueError):
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return
        speed = float(p.get("speed", 0.0))
        ts = p.get("timestamp")
        try:
            recorded = datetime.fromisoformat(ts) if ts else utcnow()
        except ValueError:
            recorded = utcnow()
        ar = ACTIVE.get(ambulance_id)
        eta = None
        leg = None
        route_id = p.get("route_id")
        if ar and route_id == str(ar.route_id):
            ar.progress_m = float(p.get("progress_m", ar.progress_m))
            eta = remaining_eta(ar)
            leg = ar.leg
        row = {"id": ambulance_id, "lat": lat, "lon": lon, "spd": speed, "ts": recorded,
               "route_id": route_id if ar and route_id == str(ar.route_id) else None}
        with self._lock:
            self.locations[ambulance_id] = row
            self.history.append(row)
        data = {"ambulance_id": ambulance_id, "latitude": lat, "longitude": lon, "speed": round(speed, 1),
                "route_id": row["route_id"], "progress_m": round(ar.progress_m, 1) if ar else None,
                "route_length_m": round(ar.total_m, 1) if ar else None, "leg": leg,
                "eta_remaining_s": None if eta is None or eta == float("inf") else round(eta, 1),
                "timestamp": recorded.isoformat()}
        self.latest[ambulance_id] = data
        broadcast_now("AMBULANCE_LOCATION_UPDATED", data)

    def handle_telemetry(self, ambulance_id: str, p: dict) -> None:
        if "fuel_level" in p:
            try:
                fuel = max(0.0, min(100.0, float(p["fuel_level"])))
            except (TypeError, ValueError):
                return
            with self._lock:
                self.fuel[ambulance_id] = fuel

    def flush(self, engine) -> dict:
        with self._lock:
            locs, hist, iot, fuel = list(self.locations.values()), self.history, self.iot, self.fuel
            self.locations, self.history, self.iot, self.fuel = {}, [], [], {}
        if not (locs or hist or iot or fuel):
            return {}
        with engine.begin() as conn:
            if locs:
                conn.execute(text(
                    "UPDATE ambulances SET latitude=CAST(:lat AS float8), longitude=CAST(:lon AS float8), "
                    "location=ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)),4326)::geography, "
                    "current_speed=CAST(:spd AS float8), last_updated=CAST(:ts AS timestamptz) WHERE id=CAST(:id AS varchar)"), locs)
            if hist:
                conn.execute(text(
                    "INSERT INTO ambulance_locations(ambulance_id, latitude, longitude, location, speed_kph, route_id, recorded_at) "
                    "SELECT a.id, CAST(:lat AS float8), CAST(:lon AS float8), "
                    "ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)),4326)::geography, "
                    "CAST(:spd AS float8), CAST(:route_id AS uuid), CAST(:ts AS timestamptz) "
                    "FROM ambulances a WHERE a.id = CAST(:id AS varchar)"), hist)
            if fuel:
                conn.execute(text("UPDATE ambulances SET fuel_level=:f WHERE id=:id"),
                             [{"id": k, "f": v} for k, v in fuel.items()])
            if iot:
                conn.execute(text("INSERT INTO iot_messages(topic, payload) VALUES (:topic, CAST(:payload AS jsonb))"), iot)
        return {"locations": len(locs), "history": len(hist), "iot": len(iot), "fuel": len(fuel)}


TELEMETRY = TelemetryBuffer()
