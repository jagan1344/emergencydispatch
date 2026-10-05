"""Test fixtures. Tests run against TEST_DATABASE_URL (default .../ems_test), which is wiped and re-created.

A small synthetic road network is generated so the suite does not depend on OSM data, OSRM or MQTT.
"""
from __future__ import annotations

import os

# configure BEFORE the app modules read settings
os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/ems_test")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.update({"MQTT_ENABLED": "false", "OSRM_URL": "", "BACKGROUND_TASKS": "false", "AUTO_DISPATCH": "false",
                   "CITY_NAME": "Testville", "CITY_LAT": "12.9716", "CITY_LON": "77.5946", "CITY_RADIUS_M": "2500",
                   "OSM_PBF_PATH": "does-not-exist.osm.pbf", "SIM_TIME_SCALE": "60", "SCENE_TIME_S": "60",
                   "HANDOVER_TIME_S": "60", "JWT_SECRET": "test-secret-for-automated-tests-only-0123456789"})

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    url = os.environ["TEST_DATABASE_URL"]
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()
    from app.ml.train import MODEL_PATH, main as train
    if not MODEL_PATH.exists():
        train(n=3000, quiet=True)
    from app.database import init_engine
    init_engine(url)
    from app.seed import seed
    result = seed(reset=False, seed_value=7, n_ambulances=8, n_hospitals=4, n_historical=10, force_synthetic=True)
    assert result["status"] == "seeded"
    from app.main import init_runtime
    init_runtime()
    yield


@pytest.fixture(scope="session")
def client(database):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def _token(client, user, pw):
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def dispatcher_headers(client):
    return _token(client, "dispatcher", "dispatch123")


@pytest.fixture(scope="session")
def viewer_headers(client):
    return _token(client, "viewer", "viewer123")


@pytest.fixture(scope="session")
def admin_headers(client):
    return _token(client, "admin", "admin123")


CRITICAL_CASE = {"emergency_type": "accident", "patient_age": 40, "heart_rate": 145, "respiratory_rate": 32,
                 "oxygen_saturation": 84, "consciousness": "UNRESPONSIVE", "bleeding": "SEVERE",
                 "injury_severity": "SEVERE", "accident_type": "ROAD", "breathing_difficulty": True, "chest_pain": False}
MILD_CASE = {"emergency_type": "other", "patient_age": 30, "heart_rate": 80, "respiratory_rate": 15,
             "oxygen_saturation": 98, "consciousness": "ALERT", "bleeding": "NONE", "injury_severity": "MINOR",
             "accident_type": "NONE", "breathing_difficulty": False, "chest_pain": False}
