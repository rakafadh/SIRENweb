"""Cuff-calibrated BP estimate (app/bp_calibrated + app/services/bp_service.py).

The sensor stream comes from the app's own simulator, fed through the real
parser, so these tests cover parsing, windows, calibration and the schedule
together. Simulated pulses only show the plumbing works, never accuracy.
"""

import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services.bp_service import BloodPressureEstimator
from app.services.monitor import systolic_for_triage
from app.services.sensor_protocol import PPG_FIELDS
from app.services.sensor_service import MockFeeder, SensorManager


def stream(seconds: int, scenario: str = "stable", manager: SensorManager | None = None, feeder: MockFeeder | None = None,
           start: int = 0, motion: float | None = None, finger: bool = True):
    """Feed `seconds` of simulated board output into a SensorManager."""
    random.seed(start + 7)
    manager = manager or SensorManager()
    feeder = feeder or MockFeeder(manager, scenario=scenario)
    for sec in range(start, start + seconds):
        for port, line in feeder.lines(float(sec)):
            if motion is not None and port == "MOCK-PPG" and line[0].isdigit():
                parts = line.split(",")          # the MPU6050 is on the PPG board
                parts[PPG_FIELDS.index("accel_rms")] = f"{motion:.4f}"
                line = ",".join(parts)
            if not finger and port == "MOCK-PPG" and line[0].isdigit():
                line = line.replace(",120000,", ",900,", 1)   # IR DC this low = no finger
            manager.handle_line(port, line)
    return manager, feeder


@pytest.fixture(scope="module")
def bp():
    est = BloodPressureEstimator()
    assert est.loaded, "model files missing from siren_app/models"
    return est


def test_model_and_settings_load(bp):
    label = bp.label()
    assert label["calibrated"] is True and label["dataset"] == "VitalDB"
    assert 0 < label["sbp_mae"] < 15 and 0 < label["dbp_mae"] < 10
    assert bp.model.schedule == "smart_kalman" and bp.model.score_z == pytest.approx(0.3)


def test_no_estimate_before_a_cuff(bp):
    bp.reset()
    manager, _ = stream(20)
    assert bp.estimate(manager, True, 2).status == "COLLECTING"
    manager, _ = stream(45, manager=manager, feeder=_, start=20)
    est = bp.estimate(manager, True, 2)
    assert est.status == "NEEDS_CALIBRATION" and est.cuff_now and not est.usable
    assert "manset" in est.message


def test_cuff_calibrates_then_estimate_is_usable(bp):
    bp.reset()
    manager, feeder = stream(80)
    cal = bp.calibrate(manager, 122, 81)
    assert cal.ok, cal
    manager, feeder = stream(30, manager=manager, feeder=feeder, start=80)
    est = bp.estimate(manager, True, 2)
    assert est.status == "OK" and est.usable and not est.cuff_now
    assert abs(est.sbp - 122) < 15 and abs(est.dbp - 81) < 10
    assert est.news2_sbp <= est.sbp                   # cautious: estimate - z sigma
    assert 0 < est.next_cuff_min <= 10                # stable patient: every 10 min


def test_cuff_start_window_is_used(bp):
    bp.reset()
    manager, feeder = stream(40)
    assert bp.mark_cuff_start(manager).ok
    # The arm is squeezed and the crew takes a while to type: pulses after the
    # start do not matter, the window kept at "Mulai ukur" calibrates
    manager, feeder = stream(10, manager=manager, feeder=feeder, start=40)
    assert bp.calibrate(manager, 118, 76).ok


def test_calibration_refused_while_moving(bp):
    bp.reset()
    manager, _ = stream(80, motion=0.30)
    cal = bp.calibrate(manager, 120, 80)
    assert not cal.ok and cal.reason == "MOTION"
    assert "bergerak" in cal.message


def test_cuff_without_diastolic_cannot_calibrate(bp):
    bp.reset()
    manager, _ = stream(80)
    assert not bp.calibrate(manager, 120, None).ok


def test_gain_step_window_is_skipped(bp):
    bp.reset()
    manager, feeder = stream(80)
    assert bp.calibrate(manager, 120, 80).ok
    first = bp.estimate(manager, True, 2)
    last_ms = manager.wave[-1][0]
    manager.handle_line("MOCK-PPG", f"G,{int(last_ms) - 5000},55")
    times, _values, why = manager.bp_window(0.0)
    assert times is None and why == "GAIN_CHANGE"
    held = bp.estimate(manager, True, 2)
    assert held.sbp == first.sbp      # the last estimate stays on screen for a minute


def test_finger_lost_clears_the_calibration(bp, monkeypatch):
    bp.reset()
    manager, feeder = stream(80)
    assert bp.calibrate(manager, 120, 80).ok
    assert bp.estimate(manager, True, 2).usable
    clock = [1000.0]
    monkeypatch.setattr("app.services.bp_service.time.time", lambda: clock[0])
    bp.estimate(manager, False, 2)
    clock[0] += 6
    est = bp.estimate(manager, False, 2)
    assert est.status == "NEEDS_CALIBRATION" and "lepas" in est.message


def test_board_restart_clears_the_calibration(bp):
    bp.reset()
    manager, feeder = stream(80)
    assert bp.calibrate(manager, 120, 80).ok
    bp.estimate(manager, True, 2)
    restarted = MockFeeder(manager)              # device clock starts again at 0
    manager, _ = stream(40, manager=manager, feeder=restarted)
    assert bp.estimate(manager, True, 2).status == "NEEDS_CALIBRATION"


def test_missing_model_is_reported_not_faked(tmp_path):
    est = BloodPressureEstimator(model_path=tmp_path / "none.joblib", settings_path=tmp_path / "none.json")
    assert not est.loaded
    manager, _ = stream(40)
    assert est.estimate(manager, True, 2).status == "NO_MODEL"
    assert not est.calibrate(manager, 120, 80).ok


# ------------------------------------------------------------ triage choice --
def _patient(systolic=None, diastolic=None, cuff_at=None):
    return SimpleNamespace(systolic=systolic, diastolic=diastolic, bp_cuff_at=cuff_at)


def _estimate(usable=True, news2_sbp=104.0, dbp=70.0, calibrated_at=1000.0):
    return SimpleNamespace(usable=usable, news2_sbp=news2_sbp, dbp=dbp, calibrated_at=calibrated_at)


def test_triage_uses_the_usable_estimate():
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(1000, tz=timezone.utc)
    assert systolic_for_triage(_patient(130, 85, at), _estimate()) == (104.0, 70.0, "PPG")


def test_triage_falls_back_to_the_cuff():
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(1000, tz=timezone.utc)
    assert systolic_for_triage(_patient(130, 85, at), _estimate(usable=False)) == (130, 85, "MANSET")
    assert systolic_for_triage(_patient(130, 85, at), None) == (130, 85, "MANSET")
    assert systolic_for_triage(_patient(), None) == (None, None, None)


def test_a_newer_cuff_that_did_not_calibrate_wins():
    from datetime import datetime, timezone
    later = datetime.fromtimestamp(1000, tz=timezone.utc) + timedelta(minutes=3)
    assert systolic_for_triage(_patient(92, 60, later), _estimate(calibrated_at=1000.0))[2] == "MANSET"


# --------------------------------------------------------- whole app (API) --
def _feed(app_module, seconds, feeder=None, start=0):
    manager = app_module.sensor_manager
    feeder = feeder or MockFeeder(manager)
    stream(seconds, manager=manager, feeder=feeder, start=start)
    return feeder


def _live(client, app_module):
    app_module.monitor._bp_cache = None
    app_module.monitor.tick()
    return client.get("/api/live").json()


def test_cuff_flow_through_the_api(client, app_module):
    patient = client.post("/api/patients", json={"acvpu": "alert", "systolic": 128, "diastolic": 84}).json()["patient"]
    feeder = _feed(app_module, 70)

    # Before any cuff taken with the finger on the sensor: the form's cuff scores NEWS2, a reminder asks for one
    live = _live(client, app_module)
    assert live["bp_estimate"]["status"] == "NEEDS_CALIBRATION"
    assert live["sbp_triage"] == {"source": "MANSET", "systolic": 128, "diastolic": 84}
    reminders = [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"]
    assert len(reminders) == 1 and reminders[0]["action"] == "cuff" and reminders[0]["level"] == "remind"

    # "Mulai ukur", then the reading
    assert client.post(f"/api/patients/{patient['id']}/cuff-start").json()["ok"]
    feeder = _feed(app_module, 5, feeder, start=70)
    body = client.patch(f"/api/patients/{patient['id']}", json={"systolic": 104, "diastolic": 70}).json()
    assert body["bp_calibration"]["ok"], body["bp_calibration"]
    assert not [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"]

    # From now on the estimate stands in for the cuff in NEWS2
    _feed(app_module, 30, feeder, start=75)
    live = _live(client, app_module)
    est = live["bp_estimate"]
    assert est["status"] == "OK" and est["usable"]
    assert live["sbp_triage"]["source"] == "PPG" and live["sbp_triage"]["systolic"] == est["news2_sbp"]
    expected = 3 if est["news2_sbp"] <= 90 else 2 if est["news2_sbp"] <= 100 else 1 if est["news2_sbp"] <= 110 else 0
    assert live["triage"]["news2"]["parts"]["sistolik"] == expected
    assert live["readiness"]["items"][-1]["state"] == "ok"


def test_failed_calibration_keeps_the_cuff_for_triage(client, app_module):
    patient = client.post("/api/patients", json={"acvpu": "alert"}).json()["patient"]
    _feed(app_module, 20)          # not yet 30 s of signal
    body = client.patch(f"/api/patients/{patient['id']}", json={"systolic": 95, "diastolic": 60}).json()
    assert body["bp_calibration"]["ok"] is False
    assert body["live"]["sbp_triage"]["source"] == "MANSET"
    assert body["live"]["triage"]["news2"]["parts"]["sistolik"] == 2


def test_same_reading_twice_still_counts_as_a_new_cuff(client, app_module):
    patient = client.post("/api/patients", json={"acvpu": "alert", "systolic": 120, "diastolic": 80}).json()["patient"]
    first = client.get(f"/api/patients/{patient['id']}").json()["patient"]["bp_cuff_at"]
    client.patch(f"/api/patients/{patient['id']}", json={"systolic": 120, "diastolic": 80})
    detail = client.get(f"/api/patients/{patient['id']}").json()
    assert detail["patient"]["bp_cuff_at"] > first
    assert len(detail["cuffs"]) == 2


def test_reminders_do_not_disturb_deterioration_alert_baseline(client, app_module):
    client.post("/api/patients", json={"acvpu": "alert", "systolic": 128, "diastolic": 84})
    _feed(app_module, 70)
    _live(client, app_module)
    # Reminders are their own kind; deterioration alerts keep reading only NOTIFIKASI events
    kinds = {n["kind"] for n in client.get("/api/notifications").json()["items"]}
    assert kinds <= {"MANSET", "MERAH", "NEWS2"}


def test_reminder_wording_follows_the_request(client, app_module):
    patient = client.post("/api/patients", json={"acvpu": "alert", "systolic": 128, "diastolic": 84}).json()["patient"]
    _feed(app_module, 70)
    _live(client, app_module)
    first = [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"]
    assert [n["title"] for n in first] == ["Ukur TD manset"]
    assert "memulai estimasi" in first[0]["message"]
    # Still no cuff five minutes later: one more, stronger reminder, and only one open at a time
    app_module.monitor._reminder_at -= 301      # as if the first reminder came five minutes ago
    _live(client, app_module)
    items = [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"]
    assert [n["title"] for n in items] == ["Ukur TD manset sekarang"]
    # The reading closes it, even when it could not calibrate (signal too short here)
    body = client.patch(f"/api/patients/{patient['id']}", json={"systolic": 120, "diastolic": 80}).json()
    assert body["bp_calibration"]["ok"] is False
    assert not [n for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"]
    # ...and asks again five minutes later
    app_module.monitor._reminder_at -= 301
    _live(client, app_module)
    assert [n["title"] for n in client.get("/api/notifications").json()["items"] if n["kind"] == "MANSET"] == ["Ukur TD manset sekarang"]
