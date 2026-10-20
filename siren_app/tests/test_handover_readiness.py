"""Handover needs every NEWS2 value, or a written reason it could not be measured."""

PPG_ROW = "1000,24.8,60,120000,0.010,1.20,78,b,30.0,12,97,16,15.0,16.0,15.5,0.4,40,nan,nan,nan,nan,nan,nan"


def _patient(client, **extra):
    return client.post("/api/patients", json={"acvpu": "alert", **extra}).json()["patient"]


def _readiness(client, app_module):
    app_module.monitor.tick()
    return client.get("/api/live").json()["readiness"]


def test_no_sensor_and_no_manual_blocks_handover(client, app_module):
    patient = _patient(client)
    readiness = _readiness(client, app_module)
    assert readiness["ready"] is False
    assert readiness["missing"] == 5
    assert [i["label"] for i in readiness["items"]] == ["Nadi", "SpO₂", "Laju napas", "Suhu", "TD sistolik manset"]

    page = client.get(f"/patients/{patient['id']}", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/monitor?blocked=1"

    response = client.post(f"/api/patients/{patient['id']}/handover")
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Data belum lengkap: Nadi, SpO₂, Laju napas, Suhu, TD sistolik manset.")
    assert client.get(f"/api/patients/{patient['id']}").json()["patient"]["status"] == "ACTIVE"


def test_manual_values_complete_the_record(client, app_module):
    patient = _patient(client, systolic=120, diastolic=80)
    client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"hr": 88, "spo2": 96, "rr": 18, "temp": 36.9})
    assert _readiness(client, app_module)["ready"] is True
    assert client.get(f"/patients/{patient['id']}", follow_redirects=False).status_code == 200
    assert client.post(f"/api/patients/{patient['id']}/handover").status_code == 200


def test_unmeasurable_value_needs_a_reason_and_is_recorded(client, app_module):
    patient = _patient(client)
    app_module.sensor_manager.handle_line("/dev/ttyUSB0", PPG_ROW)   # hr, spo2, rr from the sensor

    too_short = client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": "temp", "reason": "x"})
    assert too_short.status_code == 422
    assert too_short.json()["errors"]["reason"] == "Alasan minimal 3 karakter."

    for param, reason in [("temp", "Termometer rusak"), ("systolic", "Manset tidak tersedia")]:
        response = client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": param, "reason": reason})
        assert response.status_code == 200
    readiness = response.json()["live"]["readiness"]
    assert readiness["ready"] is True
    assert {i["param"]: i["state"] for i in readiness["items"]}["temp"] == "excused"

    assert client.post(f"/api/patients/{patient['id']}/handover").status_code == 200
    detail = client.get(f"/api/patients/{patient['id']}").json()
    assert detail["unmeasured"] == {"temp": "Termometer rusak", "systolic": "Manset tidak tersedia"}
    assert any(e["detail"] == "Suhu tidak dapat diukur: Termometer rusak" for e in detail["events"])
    assert client.get(f"/api/patients/{patient['id']}/handover.pdf").status_code == 200


def test_withdrawing_a_reason_blocks_again(client, app_module):
    patient = _patient(client, systolic=120)
    app_module.sensor_manager.handle_line("/dev/ttyUSB0", PPG_ROW)
    client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": "temp", "reason": "Sensor suhu lepas"})
    assert _readiness(client, app_module)["ready"] is True

    client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": "temp", "reason": None})
    readiness = _readiness(client, app_module)
    assert readiness["ready"] is False
    assert readiness["missing"] == 1


def test_reasons_survive_a_restart(client, app_module):
    patient = _patient(client)
    client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": "temp", "reason": "Termometer rusak"})
    app_module.monitor.patient_id = None   # what a restart looks like to the monitor
    items = {i["param"]: i for i in _readiness(client, app_module)["items"]}
    assert items["temp"]["state"] == "excused"
    assert items["temp"]["reason"] == "Termometer rusak"


def test_handed_over_record_stays_viewable(client, app_module):
    patient = _patient(client, systolic=120)
    client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"hr": 88, "spo2": 96, "rr": 18, "temp": 36.9})
    client.post(f"/api/patients/{patient['id']}/handover")
    assert client.get(f"/patients/{patient['id']}", follow_redirects=False).status_code == 200
    locked = client.post(f"/api/patients/{patient['id']}/unmeasured", json={"param": "temp", "reason": "Terlambat"})
    assert locked.status_code == 409
