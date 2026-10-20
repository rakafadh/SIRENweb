"""Line formats sent by the two SIREN boards over USB serial.

PPG node (src/ppg/main_ppg.cpp, CSV_OUTPUT 1), about once a second:
    millis,fs,led,ir_dc,drift,pi,hr,hr_src,sdnn,intervals,spo2,rr,riiv,riav,
    rifv,spread,beats,tobj,tamb,tcore,accel_rms,accel_peak,gyro_rms
    -1 marks a value the firmware judged unusable; nan marks a missing one.
    The temperature columns arrive over ESP-NOW from the other board.

PPG node (WAVEFORM_OUTPUT 1), right after each CSV row:
    W,<millis at end of block>,<fs>,<ir_1>,...,<ir_n>      raw IR counts

Temperature node (src/temp/main_temp.cpp, CSV_OUTPUT 1), once a second:
    T,millis,tobj,tamb,tcore,traw,accel_rms,accel_peak,gyro_rms,flags

Temperature node, older firmware (human-readable console, every 2 s):
    t=  12.4s  Tcore=36.82C(raw 35.10)  Tamb=29.40C  accel=0.012g peak=...

Everything else (boot banner, register dump, CSV header) is ignored.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

PPG_FIELDS = [
    "millis", "fs", "led", "ir_dc", "drift", "pi", "hr", "hr_src", "sdnn",
    "intervals", "spo2", "rr", "riiv", "riav", "rifv", "spread", "beats",
    "tobj", "tamb", "tcore", "accel_rms", "accel_peak", "gyro_rms",
]
TEMP_FIELDS = ["millis", "tobj", "tamb", "tcore", "traw", "accel_rms", "accel_peak", "gyro_rms", "flags"]

SENTINEL = -1.0
FINGER_IR_MIN = 50000     # same threshold as the firmware
TFLAG_SENSOR_OK = 0x01
TFLAG_SETTLED = 0x02
TFLAG_IMU_OK = 0x04

_LEGACY_TCORE = re.compile(r"Tcore=(-?[\d.]+|nan)C", re.IGNORECASE)
_LEGACY_TAMB = re.compile(r"Tamb=(-?[\d.]+|nan)C", re.IGNORECASE)
_LEGACY_ACCEL = re.compile(r"accel=(-?[\d.]+)g", re.IGNORECASE)


@dataclass
class ParsedLine:
    kind: str                 # "PPG" | "WAVE" | "TEMP"
    data: dict[str, Any]


def to_float(text: str | None) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def clean(value: float | None, lo: float | None = None, hi: float | None = None) -> float | None:
    """Drop the firmware's -1 sentinel and anything outside a plausible range."""
    if value is None or value <= SENTINEL:
        return None
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    return value


BODY_TEMP_RANGE = (30.0, 43.0)


def _body_temp(value: float | None) -> float | None:
    # Narrower than tools/siren_dashboard.py (20-45). With nothing in front of
    # it the sensor reports cabin air plus the calibration offset, about 29-31 C
    # in the log, which NEWS2 would score as hypothermia (3 points).
    lo, hi = BODY_TEMP_RANGE
    return value if value is not None and lo <= value <= hi else None


def parse_ppg(line: str) -> dict[str, Any] | None:
    parts = line.strip().split(",")
    if len(parts) != len(PPG_FIELDS) or not parts[0].strip().isdigit():
        return None
    raw = dict(zip(PPG_FIELDS, parts))
    ir_dc = to_float(raw["ir_dc"])
    return {
        "device_ms": int(parts[0]),
        "fs": to_float(raw["fs"]),
        "ir_dc": ir_dc,
        "finger": ir_dc is not None and ir_dc > FINGER_IR_MIN,
        "drift": to_float(raw["drift"]),
        "pi": to_float(raw["pi"]),
        "hr": clean(to_float(raw["hr"]), 20, 250),
        "hr_src": raw["hr_src"].strip(),
        "spo2": clean(to_float(raw["spo2"]), 50, 100),
        "rr": clean(to_float(raw["rr"]), 4, 60),
        "beats": to_float(raw["beats"]),
        # Temperature and motion relayed from the other board over ESP-NOW
        "tcore": _body_temp(to_float(raw["tcore"])),
        "tamb": to_float(raw["tamb"]),
        "accel_rms": to_float(raw["accel_rms"]),
        "accel_peak": to_float(raw["accel_peak"]),
        "gyro_rms": to_float(raw["gyro_rms"]),
    }


def parse_wave(line: str) -> dict[str, Any] | None:
    if not line.startswith("W,"):
        return None
    parts = line.strip().split(",")
    if len(parts) < 4:
        return None
    try:
        end_ms = int(parts[1])
        samples = [float(v) for v in parts[3:]]
    except ValueError:
        return None
    fs = to_float(parts[2])
    if fs is None or fs <= 0:
        return None
    return {"device_ms": end_ms, "fs": fs, "samples": samples}


def parse_temp(line: str) -> dict[str, Any] | None:
    if not line.startswith("T,"):
        return None
    parts = line.strip().split(",")[1:]
    if len(parts) != len(TEMP_FIELDS):
        return None
    raw = dict(zip(TEMP_FIELDS, parts))
    try:
        flags = int(raw["flags"])
        device_ms = int(raw["millis"])
    except ValueError:
        return None
    sensor_ok = bool(flags & TFLAG_SENSOR_OK)
    return {
        "device_ms": device_ms,
        "tcore": _body_temp(to_float(raw["tcore"])) if sensor_ok else None,
        "tobj": to_float(raw["tobj"]),
        "tamb": to_float(raw["tamb"]),
        "accel_rms": to_float(raw["accel_rms"]) if flags & TFLAG_IMU_OK else None,
        "accel_peak": to_float(raw["accel_peak"]) if flags & TFLAG_IMU_OK else None,
        "gyro_rms": to_float(raw["gyro_rms"]) if flags & TFLAG_IMU_OK else None,
        "sensor_ok": sensor_ok,
        "settled": bool(flags & TFLAG_SETTLED),
        "format": "csv",
    }


def parse_temp_legacy(line: str) -> dict[str, Any] | None:
    match = _LEGACY_TCORE.search(line)
    if not match:
        return None
    tamb = _LEGACY_TAMB.search(line)
    accel = _LEGACY_ACCEL.search(line)
    return {
        "device_ms": None,
        "tcore": _body_temp(to_float(match.group(1))),
        "tobj": None,
        "tamb": to_float(tamb.group(1)) if tamb else None,
        "accel_rms": to_float(accel.group(1)) if accel else None,
        "accel_peak": None,
        "gyro_rms": None,
        "sensor_ok": "[no temp sensor]" not in line,
        "settled": "[settling]" not in line,
        "format": "legacy",
    }


def parse_line(raw: str | bytes | None) -> ParsedLine | None:
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return None
    if text[0].isdigit():
        data = parse_ppg(text)
        return ParsedLine("PPG", data) if data else None
    if text.startswith("W,"):
        data = parse_wave(text)
        return ParsedLine("WAVE", data) if data else None
    if text.startswith("T,"):
        data = parse_temp(text)
        return ParsedLine("TEMP", data) if data else None
    data = parse_temp_legacy(text)
    return ParsedLine("TEMP", data) if data else None
