"""A manual respiratory rate holds until the sensor is valid again."""

import time

from tests.test_api_flow import FakeClock, feed, live, new_patient


def rr_events(client, patient_id):
    from sqlalchemy import select

    from app.database import new_session
    from app.models import PatientEvent

    db = new_session()
    try:
        rows = db.scalars(select(PatientEvent).where(
            PatientEvent.patient_id == patient_id, PatientEvent.kind == "MANUAL_VITAL")).all()
        return [{"detail": r.detail, "data": r.data} for r in rows]
    finally:
        db.close()


def no_rr(app_module):
    feed(app_module, rr=-1.0)   # the firmware's "no RR" sentinel


def test_manual_rr_does_not_expire_but_other_vitals_do(client, app_module):
    patient = new_patient(client)
    client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"rr": 18, "hr": 90})
    an_hour_ago = time.time() - 3600
    monitor = app_module.monitor
    monitor.manual = {k: (v, an_hour_ago) for k, (v, _) in monitor.manual.items()}

    vitals = live(client, app_module)["vitals"]
    assert vitals["rr"]["value"] == 18 and vitals["rr"]["source"] == "MANUAL"
    assert vitals["rr"]["until_sensor"] is True
    assert vitals["hr"]["value"] is None          # 15 min limit still applies to HR


def test_sensor_takes_over_and_releases_manual_rr_after_ten_seconds(client, app_module):
    clock = FakeClock()
    app_module.monitor.clock = clock
    patient = new_patient(client)
    no_rr(app_module)
    assert live(client, app_module)["vitals"]["rr"]["value"] is None
    client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"rr": 18})
    assert live(client, app_module)["vitals"]["rr"]["source"] == "MANUAL"

    # Valid for a few seconds: the sensor shows, the manual count is kept
    feed(app_module, rr=15.0)
    assert live(client, app_module)["vitals"]["rr"] == {"value": 15.0, "source": "SENSOR", "flags": []}
    clock.now += 4
    live(client, app_module)
    no_rr(app_module)
    back = live(client, app_module)["vitals"]["rr"]
    assert (back["value"], back["source"]) == (18, "MANUAL")

    # Valid for ten seconds without a gap: the manual value is let go
    feed(app_module, rr=15.0)
    for _ in range(11):
        live(client, app_module)
        clock.now += 1
    live(client, app_module)
    assert "rr" not in app_module.monitor.manual
    assert any("dilepas, sensor valid lagi" in e["detail"] for e in rr_events(client, patient["id"]))

    no_rr(app_module)
    assert live(client, app_module)["vitals"]["rr"]["value"] is None   # ask for a new count


def test_held_manual_rr_survives_a_restart(client, app_module):
    patient = new_patient(client)
    client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"rr": 22})
    monitor = app_module.monitor
    monitor.patient_id = None          # as after a restart: reload from the events
    monitor.manual.clear()
    no_rr(app_module)
    vitals = live(client, app_module)["vitals"]
    assert (vitals["rr"]["value"], vitals["rr"]["source"]) == (22, "MANUAL")
