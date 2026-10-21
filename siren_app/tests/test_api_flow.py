"""End to end: pickup, sensor data, crew input, deterioration, handover, PDF."""

import time
from datetime import datetime

PPG_ROW = "{ms},24.8,60,120000,0.010,1.20,{hr},b,30.0,12,{spo2},{rr},15.0,16.0,15.5,0.4,40,nan,nan,nan,nan,nan,nan"
TEMP_ROW = "T,{ms},35.40,29.50,{temp},35.20,{motion},0.0700,1.90,7"


def feed(app_module, hr=78, spo2=97, rr=16, temp=36.8, motion=0.02, ms=1000):
    sm = app_module.sensor_manager
    sm.handle_line("/dev/ttyUSB0", PPG_ROW.format(ms=ms, hr=hr, spo2=spo2, rr=rr))
    sm.handle_line("/dev/ttyACM0", TEMP_ROW.format(ms=ms, temp=temp, motion=motion))


def new_patient(client, **overrides):
    body = {"acvpu": "alert", "ambulatory": True, "systolic": 125, "diastolic": 80, **overrides}
    response = client.post("/api/patients", json=body)
    assert response.status_code == 201, response.text
    return response.json()["patient"]


def live(client, app_module):
    app_module.monitor.tick()
    return client.get("/api/live").json()


def test_home_routes(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "Pantau Pasien" in home.text and "/history" in home.text and "/sensors" in home.text
    assert client.get("/pantau", follow_redirects=False).headers["location"] == "/start"
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/"
    assert client.get("/start").status_code == 200
    assert client.get("/sensors").status_code == 200


def test_patient_without_identity_is_accepted(client):
    patient = new_patient(client, systolic=None, diastolic=None)
    assert patient["display_name"] == "Tanpa identitas"
    assert patient["code"].startswith("SRN-")
    assert client.get("/pantau", follow_redirects=False).headers["location"] == "/monitor"
    assert "Lanjutkan pemantauan" in client.get("/").text


def test_only_one_active_patient(client):
    new_patient(client)
    response = client.post("/api/patients", json={"acvpu": "alert"})
    assert response.status_code == 409


def test_sensor_values_drive_triage_with_sources(client, app_module):
    new_patient(client)
    feed(app_module)
    data = live(client, app_module)
    assert data["vitals"]["hr"] == {"value": 78.0, "source": "SENSOR", "flags": []}
    assert data["vitals"]["temp"]["via"] == "USB"
    assert data["triage"]["news2"]["measured"] == 7
    assert data["triage"]["colour"] == "HIJAU"
    assert data["updated_at"].endswith("+00:00")


def test_unresponsive_patient_is_red(client, app_module):
    # The old app read "U" but checked for "Unresponsive", so this patient came out green.
    new_patient(client, acvpu="unresponsive", ambulatory=False)
    feed(app_module)
    data = live(client, app_module)
    assert data["triage"]["colour"] == "MERAH"
    assert data["triage"]["start"]["reason"] == "tidak responsif"


def test_missing_vitals_are_not_scored_as_normal(client, app_module):
    new_patient(client, systolic=None, diastolic=None)
    data = live(client, app_module)   # no sensor lines at all
    assert data["vitals"]["hr"]["value"] is None
    assert data["triage"]["news2"]["parts"]["nadi"] is None
    assert data["triage"]["news2"]["measured"] == 2
    assert any("belum lengkap" in a for a in data["triage"]["alerts"])


def test_cuff_pressure_feeds_news2_and_model_estimate_does_not(client, app_module):
    patient = new_patient(client, systolic=None, diastolic=None)
    feed(app_module)
    assert live(client, app_module)["triage"]["news2"]["parts"]["sistolik"] is None

    response = client.patch(f"/api/patients/{patient['id']}", json={"systolic": 88, "diastolic": 55})
    assert response.status_code == 200
    triage = response.json()["live"]["triage"]
    assert triage["news2"]["parts"]["sistolik"] == 3
    assert triage["shock_index"] == round(78 / 88, 2)


def test_input_validation(client):
    patient = new_patient(client)
    assert client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"spo2": 150}).status_code == 422
    assert client.patch(f"/api/patients/{patient['id']}", json={"systolic": 80, "diastolic": 95}).status_code == 422
    assert client.patch(f"/api/patients/{patient['id']}", json={"acvpu": "U"}).status_code == 422
    assert client.post("/api/patients", json={"age": -1}).status_code == 422


def test_manual_vitals_fill_gaps_and_do_not_leak_to_next_patient(client, app_module):
    first = new_patient(client)
    response = client.post(f"/api/patients/{first['id']}/manual-vitals", json={"hr": 120, "rr": 24})
    assert response.status_code == 200
    data = response.json()["live"]
    assert data["vitals"]["hr"]["source"] == "MANUAL"
    assert data["vitals"]["hr"]["value"] == 120

    # A live sensor value wins over the manual one
    feed(app_module, hr=90)
    assert live(client, app_module)["vitals"]["hr"] == {"value": 90.0, "source": "SENSOR", "flags": []}

    assert client.post(f"/api/patients/{first['id']}/handover").status_code == 200
    app_module.sensor_manager.ppg = type(app_module.sensor_manager.ppg)()   # sensor unplugged
    second = new_patient(client)
    data = live(client, app_module)
    assert data["patient"]["id"] == second["id"]
    assert data["vitals"]["hr"]["value"] is None
    assert data["vitals"]["rr"]["value"] is None


def test_vibration_flags_hr_and_rr(client, app_module):
    new_patient(client)
    feed(app_module, motion=0.4)
    vitals = live(client, app_module)["vitals"]
    assert "getaran tinggi" in vitals["hr"]["flags"]
    assert "getaran tinggi" in vitals["rr"]["flags"]
    assert vitals["spo2"]["flags"] == []


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def test_deterioration_is_recorded_and_upgrades(client, app_module):
    patient = new_patient(client)
    clock = app_module.monitor.clock = FakeClock()
    feed(app_module)
    live(client, app_module)
    clock.now += 70
    feed(app_module, hr=112, spo2=95, ms=71000)   # NEWS2 0 -> 3: no single 3, total < 5
    data = live(client, app_module)
    assert data["triage"]["delta"] == 3
    assert data["triage"]["colour"] == "KUNING"
    assert data["triage"]["source"].startswith("ΔNEWS2")

    detail = client.get(f"/api/patients/{patient['id']}").json()
    kinds = [e["kind"] for e in detail["events"]]
    assert "KATEGORI" in kinds
    assert detail["worst_colour"] == "KUNING"


def test_undoing_a_mistaken_input_does_not_keep_the_colour_held(client, app_module):
    patient = new_patient(client)
    feed(app_module)
    assert live(client, app_module)["triage"]["colour"] == "HIJAU"
    pressed = client.patch(f"/api/patients/{patient['id']}", json={"acvpu": "unresponsive"}).json()
    assert pressed["live"]["triage"]["colour"] == "MERAH"

    undone = client.patch(f"/api/patients/{patient['id']}?undo=true", json={"acvpu": "alert"}).json()
    assert undone["live"]["triage"]["colour"] == "HIJAU"
    assert undone["live"]["triage"]["hold_remaining_s"] is None
    events = client.get(f"/api/patients/{patient['id']}").json()["events"]
    assert any(e["detail"].startswith("Diurungkan: Kesadaran") for e in events)


def test_ticking_oxygen_raises_the_score_but_is_not_deterioration(client, app_module):
    patient = new_patient(client)
    clock = app_module.monitor.clock = FakeClock()
    feed(app_module, hr=112, spo2=95)               # NEWS2 3, green
    assert live(client, app_module)["triage"]["colour"] == "HIJAU"
    clock.now += 70
    feed(app_module, hr=112, spo2=95, ms=71000)
    triage = client.patch(f"/api/patients/{patient['id']}", json={"on_oxygen": True}).json()["live"]["triage"]
    assert triage["news2"]["total"] == 5
    assert triage["delta"] == 0
    assert triage["colour"] == "KUNING"              # the report's literal rule would show MERAH


def test_colour_is_held_before_stepping_down(client, app_module):
    patient = new_patient(client)
    clock = app_module.monitor.clock = FakeClock()
    feed(app_module, hr=135, spo2=90, rr=26)         # 3 + 3 + 3 = 9, red
    assert live(client, app_module)["triage"]["colour"] == "MERAH"

    for second in range(1, 20):
        clock.now += 1
        feed(app_module, ms=1000 + second * 1000)    # back to normal
        triage = live(client, app_module)["triage"]
    assert triage["colour"] == "MERAH"
    assert triage["raw_colour"] == "HIJAU"
    assert 0 < triage["hold_remaining_s"] <= 11

    clock.now += 12
    feed(app_module, ms=40000)
    triage = live(client, app_module)["triage"]
    assert triage["colour"] == "HIJAU" and triage["hold_remaining_s"] is None

    changes = [e for e in client.get(f"/api/patients/{patient['id']}").json()["events"] if e["kind"] == "KATEGORI"]
    # One change up when the sensor arrived, one down after the hold. Nothing in between.
    assert [c["detail"].split(" (")[0] for c in changes] == [
        "Kategori berubah HIJAU → MERAH",
        "Kategori berubah MERAH → HIJAU",
    ]
    assert changes[1]["detail"].endswith("(sumber: stabil 30 detik)")


def test_handover_locks_record_and_exports_pdf(client, app_module):
    patient = new_patient(client, name="Rina Marlina", age=67, complaint="Jatuh dari motor", destination="RSUD Depok")
    feed(app_module)
    live(client, app_module)
    client.post(f"/api/patients/{patient['id']}/notes", json={"text": "Bidai lengan kiri"})

    response = client.post(f"/api/patients/{patient['id']}/handover")
    assert response.json()["patient"]["status"] == "HANDED_OVER"
    assert client.patch(f"/api/patients/{patient['id']}", json={"pain": 3}).status_code == 409
    assert client.get("/pantau", follow_redirects=False).headers["location"] == "/start"

    detail = client.get(f"/api/patients/{patient['id']}").json()
    assert any(e["kind"] == "TINDAKAN" for e in detail["events"])
    assert detail["timeline"], "handover should leave at least one snapshot"
    # 67 years old: the work-up modifier lifts green to yellow
    assert detail["latest_triage"]["colour"] == "KUNING"

    pdf = client.get(f"/api/patients/{patient['id']}/handover.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert "attachment" in pdf.headers["content-disposition"]
    assert client.get(f"/patients/{patient['id']}").status_code == 200


def test_csv_exports(client, app_module):
    new_patient(client)
    feed(app_module)
    live(client, app_module)
    readings = client.get("/api/export/readings.csv")
    assert readings.status_code == 200
    assert readings.headers["content-type"].startswith("text/csv")
    assert len(readings.text.strip().splitlines()) >= 2
    datetime.fromisoformat(readings.text.splitlines()[1].split(",")[1])
