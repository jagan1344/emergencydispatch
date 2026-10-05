"""MQTT bridge between the backend and the IoT simulation layer (Mosquitto broker).

Topics (JSON payloads):
  ambulance/{id}/location   sim → backend   GPS fix (lat, lon, speed, route progress)
  ambulance/{id}/status     sim → backend   ROUTE_STARTED / ARRIVED events
  ambulance/{id}/telemetry  sim → backend   fuel level, engine data
  ambulance/{id}/command    backend → sim   FOLLOW_ROUTE / IDLE (retained)
  traffic/{road_id}/status  both            congestion level / blocked / incident multiplier (retained)
  traffic/{road_id}/speed   sim → backend   measured average speed
  traffic/events            both            human-readable traffic incident feed
  emergency/{id}/created    backend → *     new incident notification
  emergency/{id}/status     backend → *     incident status changes
  hospital/{id}/capacity    both            capacity updates (backend) / discharge events (sim)
  simulator/heartbeat       sim → backend   liveness of the simulator processes
"""
from __future__ import annotations

import json
import logging
import uuid

import paho.mqtt.client as mqtt

log = logging.getLogger("app.mqtt")

SUBSCRIPTIONS = [
    ("ambulance/+/location", 0), ("ambulance/+/status", 1), ("ambulance/+/telemetry", 0),
    ("traffic/+/status", 1), ("traffic/+/speed", 0), ("traffic/events", 0),
    ("hospital/+/capacity", 1), ("simulator/heartbeat", 0),
]


class MqttBridge:
    def __init__(self, host: str, port: int, on_message, on_connected=None):
        self.host, self.port = host, port
        self._on_connected = on_connected
        self._handler = on_message
        self.connected = False
        self.last_error: str | None = None
        self.received = 0
        self.published = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"ems-backend-{uuid.uuid4().hex[:8]}", clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 10)

    def start(self) -> None:
        try:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()
        except Exception as exc:
            self.last_error = str(exc)
            log.error("mqtt connect failed", extra={"event": "MQTT_UNAVAILABLE", "fields": {"error": str(exc)}})

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            client.subscribe(SUBSCRIPTIONS)
            log.info("mqtt connected", extra={"event": "MQTT_CONNECTED", "fields": {"host": self.host}})
            if self._on_connected:
                try:
                    self._on_connected()
                except Exception:
                    log.exception("on_connected hook failed")
        else:
            self.last_error = str(reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        self.last_error = f"disconnected: {reason_code}"
        log.warning("mqtt disconnected", extra={"event": "MQTT_DISCONNECTED", "fields": {"reason": str(reason_code)}})

    def _on_message(self, client, userdata, msg):
        self.received += 1
        try:
            payload = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("invalid mqtt payload", extra={"fields": {"topic": msg.topic}})
            return
        try:
            self._handler(msg.topic, payload, bool(msg.retain))
        except Exception:
            log.exception("mqtt handler error", extra={"fields": {"topic": msg.topic}})

    def publish(self, topic: str, payload: dict | None, retain: bool = False, qos: int = 1) -> bool:
        if not self.connected:
            return False
        body = json.dumps(payload, default=str) if payload is not None else None
        info = self.client.publish(topic, body, qos=qos, retain=retain)
        self.published += 1
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def status(self) -> dict:
        return {"connected": self.connected, "host": f"{self.host}:{self.port}", "received": self.received,
                "published": self.published, "last_error": self.last_error}


def publish(topic: str, payload: dict | None, retain: bool = False) -> bool:
    from app.services.state import STATE
    bridge = STATE.mqtt
    if bridge is None:
        return False
    return bridge.publish(topic, payload, retain=retain)

