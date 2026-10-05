"""Routes incoming MQTT messages to the services."""
from __future__ import annotations

import logging
import time

from app.database import session_scope
from app.services.state import STATE
from app.services.telemetry_service import TELEMETRY

log = logging.getLogger("app.mqtt.handlers")


def on_message(topic: str, payload: dict, retained: bool) -> None:
    parts = topic.split("/")
    if not retained:
        TELEMETRY.record_iot(topic, payload)
    if parts[0] == "ambulance" and len(parts) == 3:
        amb_id, kind = parts[1], parts[2]
        if kind == "location":
            TELEMETRY.handle_location(amb_id, payload)
        elif kind == "telemetry":
            TELEMETRY.handle_telemetry(amb_id, payload)
        elif kind == "status" and payload.get("source") != "BACKEND" and not retained:
            from app.services.mission_service import handle_sim_status
            with STATE.lock, session_scope() as db:
                handle_sim_status(db, amb_id, payload)
    elif parts[0] == "traffic" and len(parts) == 3 and parts[2] == "status":
        # Only sensor observations from the traffic simulator change state; our own publications echo back
        # with another source, and retained messages are stale on restart (PostGIS is the source of truth).
        if payload.get("source") != "SIMULATOR" or retained:
            return
        from app.services.traffic_service import TrafficError, apply_update
        try:
            with session_scope() as db:
                apply_update(db, parts[1], level=payload.get("congestion_level"), blocked=payload.get("blocked"),
                             incident_multiplier=payload.get("incident_multiplier"),
                             event_type=payload.get("event_type", "CONGESTION"), source="SIMULATOR", publish_mqtt=False)
        except TrafficError as exc:
            log.warning("traffic update rejected", extra={"fields": {"topic": topic, "error": str(exc)}})
    elif parts[0] == "hospital" and len(parts) == 3 and parts[2] == "capacity":
        if payload.get("source") == "SIMULATOR" and payload.get("event") == "DISCHARGE" and not retained:
            from app.services.mission_service import hospital_discharge
            with session_scope() as db:
                hospital_discharge(db, parts[1], int(payload.get("count", 1)))
    elif topic == "simulator/heartbeat":
        if payload.get("component") == "traffic":
            STATE.traffic_sim_last_seen = time.time()
        else:
            STATE.simulator_last_seen = time.time()
