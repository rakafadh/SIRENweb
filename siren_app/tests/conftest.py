import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import time, so the test database and the switches
# have to be in the environment before anything from app is imported.
_TMP = Path(tempfile.mkdtemp(prefix="siren-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SIREN_BACKGROUND"] = "false"
os.environ["MOCK_SENSORS"] = "false"
os.environ["SNAPSHOT_INTERVAL_S"] = "10"
os.environ["SIREN_STATE_PATH"] = str(_TMP / "runtime.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    from app import main

    return main


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.database import new_session
    from app.models import Patient, PatientEvent, SensorEvent, TriageResult, VitalReading

    with TestClient(app_module.app) as test_client:
        db = new_session()
        for model in (TriageResult, VitalReading, PatientEvent, SensorEvent, Patient):
            db.execute(delete(model))
        db.commit()
        db.close()
        app_module.monitor.patient_id = None
        app_module.monitor.latest = None
        app_module.monitor.clock = __import__("time").time
        app_module.sensor_manager.mode = "SERIAL"
        app_module.sensor_manager.scenario = "stable"
        (_TMP / "runtime.json").unlink(missing_ok=True)
        app_module.sensor_manager.ppg = type(app_module.sensor_manager.ppg)()
        app_module.sensor_manager.temp = type(app_module.sensor_manager.temp)()
        app_module.sensor_manager.wave.clear()
        yield test_client
