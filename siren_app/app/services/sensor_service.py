"""Latest sensor state, assembled from whatever the boards are sending.

Nothing here is held past SENSOR_STALE_S: a board that stops talking must show
up as missing, not as a frozen last value that looks like a working sensor.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings, settings
from app.database import new_session
from app.models import SensorEvent
from app.services.sensor_protocol import PPG_FIELDS, parse_line
from app.services.serial_hub import SerialHub

log = logging.getLogger("siren.sensors")

WAVE_BUFFER_S = 60
WAVE_MAX_FS = 100


@dataclass
class NodeState:
    port: str | None = None
    last_rx: float | None = None      # host time.time()
    data: dict[str, Any] = field(default_factory=dict)

    def age(self, now: float) -> float | None:
        return None if self.last_rx is None else now - self.last_rx


@dataclass
class PortInfo:
    role: str | None = None           # PPG | TEMP | None until identified
    state: str = "OPENED"             # OPENED | OPEN_FAILED | LOST | CLOSED
    last_rx: float | None = None
    lines: int = 0
    detail: str | None = None


MODES = ("SERIAL", "MOCK")
SCENARIOS = ("stable", "deteriorating")


def load_runtime_state(path: Path) -> dict[str, Any]:
    """The mode chosen on the Perangkat page, kept across restarts."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_runtime_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)   # atomic, so a power cut never leaves half a file


class SensorManager:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        # .env gives the starting mode; a choice made on the Perangkat page wins
        saved = load_runtime_state(cfg.STATE_PATH)
        self.mode = saved.get("sensor_mode") if saved.get("sensor_mode") in MODES else ("MOCK" if cfg.MOCK_SENSORS else "SERIAL")
        self.scenario = saved.get("mock_scenario") if saved.get("mock_scenario") in SCENARIOS else cfg.MOCK_SCENARIO
        self.ppg = NodeState()
        self.temp = NodeState()
        self.ports: dict[str, PortInfo] = {}
        self.wave: deque[tuple[float, float]] = deque(maxlen=WAVE_BUFFER_S * WAVE_MAX_FS)
        self.wave_fs: float | None = None
        self._lock = threading.RLock()
        self._reported: dict[str, str] = {}
        # Bumped on every mode switch. A line still in flight from the old
        # source carries the old number and is dropped.
        self._generation = 0
        self._running = False
        self.hub: SerialHub | None = None
        self._mock: MockFeeder | None = None

    # ------------------------------------------------------------ lifecycle --
    def start(self) -> None:
        self._running = True
        self._start_source()

    def stop(self) -> None:
        self._running = False
        self._stop_source()

    def _start_source(self) -> None:
        generation = self._generation
        if self.mode == "MOCK":
            self._mock = MockFeeder(self, self.scenario, generation)
            self._mock.start()
            return
        printer_port = self.cfg.PRINTER_DEVICE if self.cfg.PRINTER_BACKEND == "escpos" else None
        self.hub = SerialHub(
            self.cfg.SERIAL_BAUD, self.cfg.SERIAL_PORTS,
            lambda port, line: self.handle_line(port, line, generation),
            lambda port, state, detail: self._port_event(port, state, detail, generation),
            exclude=[printer_port],
        )
        self.hub.start()

    def _stop_source(self) -> None:
        if self.hub:
            self.hub.stop()   # readers close their ports within about a second
            self.hub = None
        if self._mock:
            self._mock.stop()
            self._mock = None

    def set_mode(self, mode: str, scenario: str | None = None) -> None:
        """Switch between real boards and simulation, discarding every reading so far."""
        with self._lock:
            self._stop_source()
            self._generation += 1
            self.mode = mode
            if scenario in SCENARIOS:
                self.scenario = scenario
            self.ppg = NodeState()
            self.temp = NodeState()
            self.ports.clear()
            self.wave.clear()
            self.wave_fs = None
            save_runtime_state(self.cfg.STATE_PATH, {"sensor_mode": self.mode, "mock_scenario": self.scenario})
            if self._running:
                self._start_source()
        detail = "Sensor sungguhan (ESP32)" if mode == "SERIAL" else f"Simulasi, skenario {self.scenario}"
        db = new_session()
        try:
            db.add(SensorEvent(sensor="SUMBER DATA", event_type="MODE", status=mode, details=detail))
            db.commit()
        except Exception:
            log.exception("Could not record mode change")
            db.rollback()
        finally:
            db.close()

    def rescan(self) -> None:
        if self.mode == "SERIAL" and self.hub:
            self.hub.rescan()

    # -------------------------------------------------------------- intake --
    def _port_event(self, port: str, state: str, detail: str | None, generation: int | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            info = self.ports.setdefault(port, PortInfo())
            info.state = state
            info.detail = detail

    def handle_line(self, port: str, raw: str | bytes, generation: int | None = None) -> None:
        now = time.time()
        parsed = parse_line(raw)
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            info = self.ports.setdefault(port, PortInfo())
            info.last_rx = now
            info.lines += 1
            if parsed is None:
                return
            if parsed.kind == "PPG":
                info.role = "PPG"
                self.ppg = NodeState(port, now, parsed.data)
            elif parsed.kind == "WAVE":
                info.role = "PPG"
                self._append_wave(parsed.data)
            elif parsed.kind == "TEMP":
                info.role = "TEMP"
                self.temp = NodeState(port, now, parsed.data)

    def _append_wave(self, data: dict[str, Any]) -> None:
        fs = data["fs"]
        step = 1000.0 / fs
        samples = data["samples"]
        end = float(data["device_ms"])
        times = [end - (len(samples) - 1 - i) * step for i in range(len(samples))]
        if self.wave:
            last = self.wave[-1][0]
            # A hole in the stream (lost lines) or the board's clock going
            # backwards (it reset) breaks continuity. Start a fresh window.
            if times[0] - last > 3 * step or times[0] < last - step:
                self.wave.clear()
        self.wave.extend(zip(times, samples))
        self.wave_fs = fs

    # ------------------------------------------------------------- readout --
    def _fresh(self, node: NodeState, now: float) -> bool:
        age = node.age(now)
        return age is not None and age <= self.cfg.SENSOR_STALE_S

    def node_status(self, node: NodeState, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if node.last_rx is None:
            return "NOT_FOUND"
        return "CONNECTED" if self._fresh(node, now) else "STALE"

    def readings(self) -> dict[str, Any]:
        """Current sensor values. None means not available right now."""
        now = time.time()
        with self._lock:
            ppg = self.ppg.data if self._fresh(self.ppg, now) else {}
            temp_usb = self.temp.data if self._fresh(self.temp, now) else {}

        temp, temp_via = None, None
        if temp_usb.get("tcore") is not None:
            temp, temp_via = temp_usb["tcore"], "USB"
        elif ppg.get("tcore") is not None:
            temp, temp_via = ppg["tcore"], "ESP-NOW"

        motion = temp_usb.get("accel_rms")
        if motion is None:
            motion = ppg.get("accel_rms")

        return {
            "hr": ppg.get("hr"),
            "spo2": ppg.get("spo2"),
            "rr": ppg.get("rr"),
            "temp": temp,
            "pi": ppg.get("pi"),
            "finger": ppg.get("finger"),
            "ir_dc": ppg.get("ir_dc"),
            "motion_g": motion,
            "temp_via": temp_via,
            "ppg_connected": bool(ppg),
            "temp_connected": temp is not None,
        }

    def waveform(self, window_s: float) -> tuple[np.ndarray, np.ndarray, float | None]:
        """The newest window_s seconds of raw IR, as device-ms times and values."""
        with self._lock:
            if not self.wave or not self._fresh(self.ppg, time.time()):
                return np.empty(0), np.empty(0), None
            data = np.asarray(self.wave, dtype=np.float64)
            fs = self.wave_fs
        times, values = data[:, 0], data[:, 1]
        keep = times >= times[-1] - window_s * 1000.0
        return times[keep], values[keep], fs

    def status_payload(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            ppg_age = self.ppg.age(now)
            temp_age = self.temp.age(now)
            ports = [
                {
                    "port": name,
                    "role": info.role,
                    "state": info.state,
                    "lines": info.lines,
                    "age_s": None if info.last_rx is None else round(now - info.last_rx, 1),
                    "detail": info.detail,
                }
                for name, info in sorted(self.ports.items())
            ]
            wave_s = 0.0
            if len(self.wave) > 1:
                wave_s = (self.wave[-1][0] - self.wave[0][0]) / 1000.0
            ppg_status = self.node_status(self.ppg, now)
            temp_status = self.node_status(self.temp, now)
            finger = self.ppg.data.get("finger") if ppg_status == "CONNECTED" else None
        readings = self.readings()
        return {
            "mode": self.mode,
            "scenario": self.scenario,
            "ppg": {"status": ppg_status, "port": self.ppg.port, "age_s": _round(ppg_age), "finger": finger},
            "temp": {
                "status": temp_status,
                "port": self.temp.port,
                "age_s": _round(temp_age),
                # Temperature can still arrive through the PPG board over ESP-NOW
                "via": readings["temp_via"],
            },
            "ports": ports,
            "waveform_seconds": round(wave_s, 1),
            "waveform_fs": self.wave_fs,
        }

    def record_status_changes(self) -> None:
        """Write a sensor_events row whenever a board connects, drops or goes quiet."""
        now = time.time()
        with self._lock:
            current = {
                "PPG (MAX30102)": (self.node_status(self.ppg, now), self.ppg.port),
                "SUHU (MLX90614)": (self.node_status(self.temp, now), self.temp.port),
            }
        changes = [(name, status, port) for name, (status, port) in current.items() if self._reported.get(name) != status]
        if not changes:
            return
        db = new_session()
        try:
            for name, status, port in changes:
                db.add(SensorEvent(sensor=name, event_type="STATUS", status=status, port=port,
                                   details="Mode simulasi" if self.mode == "MOCK" else None))
                self._reported[name] = status
            db.commit()
        except Exception:
            log.exception("Could not record sensor event")
            db.rollback()
        finally:
            db.close()


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class MockFeeder:
    """Synthetic board output, fed through the real parser.

    Every value is invented. The UI marks the whole session as SIMULASI, and
    nothing produced here may be presented as a patient measurement.
    """

    FS = 25.0

    def __init__(self, manager: SensorManager, scenario: str = "stable", generation: int | None = None):
        self.manager = manager
        self.scenario = scenario
        self.generation = generation
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device_ms = 0
        self._phase = 0.0
        self._resp_phase = 0.0
        self._started = time.time()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mock-sensors", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def vitals(self, elapsed: float) -> dict[str, float]:
        # deteriorating: a patient sliding from normal to NEWS2 high risk over
        # five minutes, so the delta-NEWS2 upgrade can be watched happening.
        p = min(1.0, elapsed / 300.0) if self.scenario == "deteriorating" else 0.0
        jitter = random.uniform
        return {
            "hr": 78 + 57 * p + jitter(-2, 2),
            "spo2": round(97 - 8 * p + jitter(-0.6, 0.6)),
            "rr": 16 + 12 * p + jitter(-0.8, 0.8),
            "temp": 36.8 + 1.1 * p + jitter(-0.05, 0.05),
            "pi": 1.4 - 1.0 * p,
            "motion": abs(random.gauss(0.04, 0.02)),
        }

    def lines(self, elapsed: float) -> list[tuple[str, str]]:
        v = self.vitals(elapsed)
        self._device_ms += 1000
        ppg_values = {
            "millis": str(self._device_ms), "fs": f"{self.FS:.1f}", "led": "60", "ir_dc": "120000",
            "drift": "0.010", "pi": f"{v['pi']:.2f}", "hr": f"{v['hr']:.1f}", "hr_src": "b", "sdnn": "25.0",
            "intervals": "12", "spo2": str(int(v["spo2"])), "rr": f"{v['rr']:.1f}", "riiv": "nan",
            "riav": "nan", "rifv": "nan", "spread": "1.0", "beats": "40",
            # Temperature comes over the temperature board's own USB line below
            "tobj": "nan", "tamb": "nan", "tcore": "nan",
            "accel_rms": "nan", "accel_peak": "nan", "gyro_rms": "nan",
        }
        ppg_line = ",".join(ppg_values[name] for name in PPG_FIELDS)

        samples = []
        for _ in range(int(self.FS)):
            self._phase = (self._phase + v["hr"] / 60.0 / self.FS) % 1.0
            self._resp_phase = (self._resp_phase + v["rr"] / 60.0 / self.FS) % 1.0
            pulse = math.exp(-((self._phase - 0.15) / 0.06) ** 2) + 0.35 * math.exp(-((self._phase - 0.45) / 0.08) ** 2)
            # Reflectance IR dips at systole, as the real sensor does
            ir = 120000 - 900 * pulse + 250 * math.sin(2 * math.pi * self._resp_phase) + random.gauss(0, 15)
            samples.append(str(int(ir)))
        wave_line = f"W,{self._device_ms},{self.FS:.2f}," + ",".join(samples)

        temp_line = (
            f"T,{self._device_ms},{v['temp'] - 1.2:.2f},29.50,{v['temp']:.2f},{v['temp'] - 1.3:.2f},"
            f"{v['motion']:.4f},{v['motion'] * 3:.4f},2.10,7"
        )
        return [("MOCK-PPG", ppg_line), ("MOCK-PPG", wave_line), ("MOCK-SUHU", temp_line)]

    def _run(self) -> None:
        for port in ("MOCK-PPG", "MOCK-SUHU"):
            self.manager._port_event(port, "OPENED", "Simulasi", self.generation)
        while not self._stop.is_set():
            for port, line in self.lines(time.time() - self._started):
                self.manager.handle_line(port, line, self.generation)
            self._stop.wait(1.0)
