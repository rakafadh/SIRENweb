"""Switching between real boards and simulation from the Perangkat page."""

import json

from app.config import settings
from app.services.sensor_service import SensorManager

PPG_ROW = "1000,24.8,60,120000,0.010,1.20,78,b,30.0,12,97,16,15.0,16.0,15.5,0.4,40,nan,nan,nan,nan,nan,nan"


def test_default_mode_comes_from_env(client):
    state = client.get("/api/sensors/mode").json()
    assert state == {"mode": "SERIAL", "scenario": "stable", "can_switch": True, "active_patient": None}


def test_switch_clears_old_readings_and_is_saved(client, app_module):
    manager = app_module.sensor_manager
    manager.handle_line("/dev/ttyUSB0", PPG_ROW)
    assert manager.readings()["hr"] == 78

    response = client.post("/api/sensors/mode", json={"mode": "MOCK", "scenario": "deteriorating"})
    assert response.status_code == 200
    assert response.json()["mode"] == "MOCK"
    assert manager.readings()["hr"] is None              # nothing carried over
    assert manager.status_payload()["ports"] == []
    assert json.loads(settings.STATE_PATH.read_text()) == {"sensor_mode": "MOCK", "mock_scenario": "deteriorating"}

    events = client.get("/api/sensor-events").json()["items"]
    assert {"sensor": "SUMBER DATA", "status": "MOCK"} in [{"sensor": e["sensor"], "status": e["status"]} for e in events]

    # A fresh start (restart) picks the saved choice over .env
    assert SensorManager().mode == "MOCK"


def test_lines_from_the_previous_source_are_dropped(client, app_module):
    manager = app_module.sensor_manager
    old_generation = manager._generation
    client.post("/api/sensors/mode", json={"mode": "MOCK"})
    manager.handle_line("/dev/ttyUSB0", PPG_ROW, old_generation)
    assert manager.readings()["hr"] is None


def test_cannot_switch_with_an_active_patient(client, app_module):
    patient = client.post("/api/patients", json={"acvpu": "alert"}).json()["patient"]
    state = client.get("/api/sensors/mode").json()
    assert state["can_switch"] is False and state["active_patient"] == patient["code"]

    response = client.post("/api/sensors/mode", json={"mode": "MOCK"})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Ada pasien aktif ({patient['code']}). Selesaikan serah terima dulu sebelum mengganti sumber data sensor."
    )
    assert app_module.sensor_manager.mode == "SERIAL"


def test_unknown_mode_is_rejected(client):
    body = client.post("/api/sensors/mode", json={"mode": "DEMO"}).json()
    assert body["errors"]["mode"] == "Pilihan mode sensor tidak dikenal."


def test_badge_and_start_warning_follow_the_mode(client):
    page = client.get("/start").text
    assert 'class="sim-badge hidden"' in page
    assert "Sumber sensor: SIMULASI" not in page

    client.post("/api/sensors/mode", json={"mode": "MOCK"})
    page = client.get("/start").text
    assert 'class="sim-badge"' in page
    assert "Sumber sensor: SIMULASI" in page
