"""Deterioration alerts: MERAH sounds an alarm, a NEWS2 rise of 3 beeps once."""

import time

from app.services import triage as TR
from app.services.deterioration_alerts import DeteriorationWatch

CALM = {"respirasi": 0, "spo2": 0, "oksigen": 0, "sistolik": 0, "nadi": 1, "kesadaran": 0, "suhu": 0}   # NEWS2 1


def worse(**changes):
    return {**CALM, **changes}


def run(watch, start, seconds, parts, colour=TR.KUNING, total=4):
    """Feed the same evaluation once a second; return the alerts raised."""
    raised = []
    for i in range(seconds):
        alert = watch.update(start + i, parts, colour, total, 7)
        if alert:
            raised.append(alert)
    return raised


# --- unit ---------------------------------------------------------------------
def test_rise_of_three_beeps_once_after_five_seconds():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    up = worse(nadi=2, respirasi=2)                       # 1 -> 4, +3
    assert run(watch, 1, 5, up) == []                     # not yet: must hold 5 s
    alerts = run(watch, 6, 30, up)
    assert len(alerts) == 1                               # announced once, not every second
    assert alerts[0].kind == "NEWS2" and alerts[0].level == "beep"
    assert alerts[0].title == "NEWS2 naik 3 poin"
    assert "nadi +1" in alerts[0].message and "laju napas +2" in alerts[0].message
    assert "mempercepat perjalanan" in alerts[0].message


def test_reference_moves_after_an_alert():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    run(watch, 1, 10, worse(nadi=2, respirasi=2))         # 1 -> 4, alert
    assert run(watch, 11, 10, worse(nadi=3, respirasi=2)) == []        # 4 -> 5: no
    assert len(run(watch, 21, 10, worse(nadi=3, respirasi=3, spo2=1))) == 1   # 4 -> 7: alert


def test_short_spike_and_improvement_are_silent():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    assert run(watch, 1, 3, worse(spo2=3)) == []          # 3 s spike only
    assert run(watch, 4, 10, worse(nadi=0)) == []         # getting better


def test_getting_worse_again_after_recovering_is_caught():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    run(watch, 1, 10, worse(nadi=3, respirasi=1))         # alert, reference now high
    run(watch, 11, 70, CALM, colour=TR.HIJAU, total=1)    # better for over 60 s
    assert len(run(watch, 81, 10, worse(nadi=3, respirasi=1))) == 1


def test_oxygen_alone_does_not_alert():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    assert run(watch, 1, 20, worse(oksigen=2, nadi=2)) == []    # +2 oxygen, +1 pulse: rise 1


def test_turning_red_alarms_immediately_but_not_at_pickup():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.KUNING, 4, 7)
    alert = watch.update(1, CALM, TR.MERAH, 7, 7)
    assert alert.kind == "MERAH" and alert.level == "alarm"
    assert alert.message.startswith("Kategori naik dari KUNING ke MERAH.")

    at_pickup = DeteriorationWatch()
    assert at_pickup.update(0, CALM, TR.MERAH, 7, 7) is None


def test_red_and_rise_together_give_one_alarm():
    watch = DeteriorationWatch()
    watch.update(0, CALM, TR.HIJAU, 1, 7)
    for t in range(1, 6):
        watch.update(t, worse(nadi=2, respirasi=2), TR.KUNING, 4, 7)
    alert = watch.update(6, worse(nadi=3, respirasi=3, spo2=3), TR.MERAH, 10, 7)
    assert alert.kind == "MERAH"
    assert run(watch, 7, 10, worse(nadi=3, respirasi=3, spo2=3), colour=TR.MERAH, total=10) == []


# --- API ----------------------------------------------------------------------
PPG_ROW = "{ms},24.8,60,120000,0.010,1.20,{hr},b,30.0,12,{spo2},{rr},15.0,16.0,15.5,0.4,40,nan,nan,nan,nan,nan,nan"
TEMP_ROW = "T,{ms},35.40,29.50,36.80,35.20,0.0200,0.0700,1.90,7"


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def feed(app_module, hr, spo2, rr, ms):
    app_module.sensor_manager.handle_line("/dev/ttyUSB0", PPG_ROW.format(ms=ms, hr=hr, spo2=spo2, rr=rr))
    app_module.sensor_manager.handle_line("/dev/ttyACM0", TEMP_ROW.format(ms=ms))


def test_alert_reaches_the_screen_and_is_acknowledged(client, app_module):
    clock = app_module.monitor.clock = FakeClock()
    patient = client.post("/api/patients", json={"acvpu": "alert", "systolic": 125, "diastolic": 80}).json()["patient"]
    feed(app_module, 85, 97, 16, 1000)                    # NEWS2 0
    app_module.monitor.tick()
    assert client.get("/api/notifications").json()["items"] == []

    for second in range(1, 9):                            # nadi 112 (+2), napas 22 (+2): rise 4
        clock.now += 1
        feed(app_module, 112, 97, 22, 1000 + second * 1000)
        app_module.monitor.tick()
    items = client.get("/api/notifications").json()["items"]
    assert len(items) == 1
    assert items[0]["level"] == "beep" and items[0]["title"] == "NEWS2 naik 4 poin"
    assert items[0]["patient_code"] == patient["code"]

    response = client.post(f"/api/notifications/{items[0]['id']}/ack")
    assert response.status_code == 200
    assert client.get("/api/notifications").json()["items"] == []
    events = client.get(f"/api/patients/{patient['id']}").json()["events"]
    note = next(e for e in events if e["kind"] == "NOTIFIKASI")
    assert "Dikonfirmasi petugas" in note["detail"]

    assert client.post("/api/notifications/tidak-ada/ack").status_code == 404


def test_turning_red_is_an_alarm_listed_first(client, app_module):
    clock = app_module.monitor.clock = FakeClock()
    client.post("/api/patients", json={"acvpu": "alert", "systolic": 125, "diastolic": 80})
    feed(app_module, 85, 97, 16, 1000)
    app_module.monitor.tick()
    clock.now += 1
    feed(app_module, 135, 90, 26, 2000)                   # 3 + 3 + 3: MERAH
    app_module.monitor.tick()
    items = client.get("/api/notifications").json()["items"]
    assert items[0]["level"] == "alarm"
    assert items[0]["title"] == "Pasien memburuk: kategori MERAH"


def test_reference_survives_a_restart(client, app_module):
    clock = app_module.monitor.clock = FakeClock()
    client.post("/api/patients", json={"acvpu": "alert", "systolic": 125, "diastolic": 80})
    feed(app_module, 85, 97, 16, 1000)
    app_module.monitor.tick()
    for second in range(1, 9):
        clock.now += 1
        feed(app_module, 112, 97, 22, 1000 + second * 1000)
        app_module.monitor.tick()
    assert len(client.get("/api/notifications").json()["items"]) == 1

    app_module.monitor.patient_id = None                  # what a restart looks like
    for second in range(9, 20):
        clock.now += 1
        feed(app_module, 112, 97, 22, 1000 + second * 1000)
        app_module.monitor.tick()
    # Same scores as already announced: no second alert after the restart
    assert len(client.get("/api/notifications").json()["items"]) == 1
