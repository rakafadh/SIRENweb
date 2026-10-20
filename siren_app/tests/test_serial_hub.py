import threading

import serial

from app.services import serial_hub
from app.services.sensor_service import SensorManager

LINES = [
    b"SIREN PPG node\r\n",
    b"52000,24.8,60,120000,0.010,1.20,76.4,b,30.0,12,97,15.5,15.0,16.0,15.5,0.4,40,nan,nan,nan,nan,nan,nan\r\n",
    b"W,52000,24.80,120100,119950,119800\r\n",
    b"T,8000,35.40,29.50,36.60,35.20,0.0210,0.0700,1.90,7\r\n",
]


class FakePort:
    """Plays back LINES, then behaves like a board being unplugged."""

    def __init__(self):
        self.lines = list(LINES)

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        raise serial.SerialException("device disconnected")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_reader_feeds_lines_and_reports_disconnect(monkeypatch):
    monkeypatch.setattr(serial_hub.serial, "serial_for_url", lambda *a, **k: FakePort())
    manager = SensorManager()
    done = threading.Event()
    events = []

    def on_event(port, state, detail):
        events.append(state)
        manager._port_event(port, state, detail)
        if state == "LOST":
            done.set()

    hub = serial_hub.SerialHub(115200, ["/dev/ttyUSB0"], manager.handle_line, on_event)
    hub.rescan()
    assert done.wait(2)

    assert events == ["OPENED", "LOST"]
    readings = manager.readings()
    assert readings["hr"] == 76.4
    assert readings["temp"] == 36.6 and readings["temp_via"] == "USB"
    # Both lines came over one port here, so the port is labelled by the last line it sent
    status = manager.status_payload()
    assert status["ppg"]["status"] == "CONNECTED"
    assert status["ports"][0]["state"] == "LOST"
    assert status["ports"][0]["lines"] == 4


def test_open_failure_is_reported(monkeypatch):
    def refuse(*args, **kwargs):
        raise serial.SerialException("permission denied")

    monkeypatch.setattr(serial_hub.serial, "serial_for_url", refuse)
    events = []
    hub = serial_hub.SerialHub(115200, ["/dev/ttyUSB9"], lambda *a: None, lambda p, s, d: events.append((s, d)))
    hub.rescan()
    for thread in threading.enumerate():
        if thread.name == "serial-/dev/ttyUSB9":
            thread.join(2)
    assert events == [("OPEN_FAILED", "permission denied")]
