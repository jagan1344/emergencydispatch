"""Shared helpers for the IoT simulators (independent of the backend code base)."""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import uuid
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE lines) so the simulator shares the project configuration."""
    for p in (ROOT / ".env", ROOT / "backend" / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(stream=sys.stdout, level=os.environ.get("LOG_LEVEL", "INFO"),
                        format='{"ts":"%(asctime)s","level":"%(levelname)s","component":"%(name)s","msg":%(message)s}')
    return logging.getLogger(name)


def jmsg(**kw) -> str:
    return json.dumps(kw, default=str)


def mqtt_client(prefix: str) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-{uuid.uuid4().hex[:6]}", clean_session=True)
    c.reconnect_delay_set(1, 10)
    return c


def backend_client() -> httpx.Client:
    """Authenticated REST client (used only to read the road / hospital lists at start-up)."""
    base = os.environ.get("BACKEND_URL", "http://localhost:8000")
    c = httpx.Client(base_url=base, timeout=15)
    r = c.post("/api/auth/login", json={"username": os.environ.get("SIM_USERNAME", "viewer"),
                                         "password": os.environ.get("SIM_PASSWORD", "viewer123")})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


CONGESTION_FACTORS = {"FREE": 1.0, "LIGHT": 0.85, "MODERATE": 0.70, "HEAVY": 0.50, "SEVERE": 0.30, "BLOCKED": 0.0}
LEVELS = list(CONGESTION_FACTORS)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360
