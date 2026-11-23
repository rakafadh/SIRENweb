"""SIREN QR payload, format version 1.

Turns one patient's transport record into a short string of digits that goes
after the '#' of the viewer URL. The fragment never leaves the phone: browsers
do not send it to the web server, so the server only ever serves a static page.

Pipeline
    record (dict) -> bucket readings into at most MAX_POINTS points
                  -> pack into bytes (layout below) + CRC32
                  -> raw DEFLATE
                  -> b'\\x01' + compressed -> big integer -> decimal digits

Digits, not base64, because a QR code has a numeric mode that stores
3 digits in 10 bits. That is about 25% denser than base64 in byte mode.

Byte layout (big-endian), version 1
    u8   version (= 1)
    u8   flags        bit0 simulated, bit1 on oxygen, bit2 handed over
    u32  start        Unix seconds of the first trend point
    u16  step_s       seconds per trend point
    u8   n            number of trend points
    i16  printed      print time, 10 s units after start
    u8   age          255 = unknown
    u8   sex          0 unknown, 1 L, 2 P
    u8   acvpu        0..4 (A C V P U), 255 = unknown
    u8   pain         0..10, 255 = unknown
    u8   colours      bits0-1 current, bits2-3 worst, bits4-5 START
                      (0 none, 1 HIJAU, 2 KUNING, 3 MERAH)
    u8   news2        255 = none
    u8   news2_measured
    u16  alerts       bitmask, see ALERTS
    u8   cuff count, then per cuff: i16 time (10 s units after start), u8 sys, u8 dia
    str  code, unit, complaint   each u8 length + UTF-8
    6 x n bytes       series HR, SpO2, RR, temp, NEWS2, colour; 255 = missing;
                      each series delta-coded modulo 256
    -- optional tail (absent in the earliest labels; decoders stop at the CRC) --
    str  name         u8 length + UTF-8 (max 32 bytes); length 0 = no name
    sections          repeated: u8 tag, u16 length, data. Unknown tags are
                      skipped, so new sections never break older viewers.
                      tag 1  PPG blood-pressure estimate: n bytes SBP then
                             n bytes DBP, mmHg, 255 = missing, delta-coded
    u32  CRC32 of everything above
"""

from __future__ import annotations

import math
import statistics
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

VERSION = 1
MAX_POINTS = 60
MISSING = 255

COLOURS = {None: 0, "HIJAU": 1, "KUNING": 2, "MERAH": 3}
COLOUR_NAMES = {v: k for k, v in COLOURS.items()}
SEXES = {None: 0, "L": 1, "P": 2}
SEX_NAMES = {v: k for k, v in SEXES.items()}
ACVPU = ["A", "C", "V", "P", "U"]

# Order matters: the bit index is part of the format. Append only.
ALERTS = [
    "shock_index",     # 0  HR/SBP >= 1.0
    "news2_rise",      # 1  NEWS2 up by >= 2 within 60 s
    "spo2_low",        # 2  SpO2 <= 91
    "rr_high",         # 3  RR >= 25
    "rr_low",          # 4  RR <= 8
    "hr_high",         # 5  HR >= 131
    "hr_low",          # 6  HR <= 40
    "sbp_low",         # 7  cuff SBP <= 90
    "temp_high",       # 8  temp >= 39.1
    "temp_low",        # 9  temp <= 35.0
    "new_confusion",   # 10 ACVPU no longer A
    "bp_trend",        # 11 PPG trend model asks for a cuff reading
]

SERIES = ["hr", "spo2", "rr", "temp", "news2", "colour"]
PPG_BP = ["sbp_ppg", "dbp_ppg"]   # tag 1
TAG_PPG_BP = 1
TEXT_LIMITS = {"code": 24, "unit": 16, "complaint": 48}
NAME_LIMIT = 32


class PayloadError(ValueError):
    pass


# ----------------------------------------------------------------- helpers --
def _ts(value: Any) -> float:
    """ISO string, datetime or Unix seconds -> Unix seconds."""
    if value is None:
        raise PayloadError("missing timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _u8(value: float | None, scale: float = 1.0, offset: float = 0.0) -> int:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return MISSING
    return max(0, min(254, round((value - offset) * scale)))


def _colour_code(name: str | None) -> int:
    if name not in COLOURS:
        raise PayloadError(f"unknown colour {name!r}")
    return COLOURS[name]


def _text(value: str | None, limit: int) -> bytes:
    raw = (value or "").strip().encode("utf-8")
    raw = raw[:limit].decode("utf-8", "ignore").encode("utf-8")   # cut on a character boundary
    return bytes([len(raw)]) + raw


def _ticks(seconds: float) -> int:
    return max(-32768, min(32767, round(seconds / 10)))


# --------------------------------------------------------------- bucketing --
@dataclass
class Trend:
    start: int
    step_s: int
    points: list[dict[str, int]] = field(default_factory=list)


def choose_step(duration_s: float, max_points: int = MAX_POINTS) -> int:
    """One point per minute, or whole minutes wide enough to stay under max_points."""
    minutes = max(1, math.ceil((duration_s + 1) / 60 / max_points))
    return 60 * minutes


def bucket(readings: list[dict[str, Any]], step_s: int | None = None,
           max_points: int = MAX_POINTS) -> Trend:
    """Readings every few seconds -> one point per step.

    Vitals use the median of the bucket (robust to a motion spike).
    NEWS2 and colour use the worst value in the bucket, so the trend never
    hides a moment that was flagged on the ambulance screen.
    """
    rows = sorted(((_ts(r["t"]), r) for r in readings), key=lambda x: x[0])
    if not rows:
        return Trend(start=0, step_s=step_s or 60)
    first, last = rows[0][0], rows[-1][0]
    step = step_s or choose_step(last - first, max_points)
    start = int(first // step * step)
    n = min(max_points, int((last - start) // step) + 1)
    if n == max_points and (last - start) // step >= max_points:
        # Too long for this step: keep the most recent window
        start = int((last // step - max_points + 1) * step)
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    for t, r in rows:
        i = int((t - start) // step)
        if 0 <= i < n:
            buckets[i].append(r)

    points = []
    for rs in buckets:
        def med(key):
            vals = [r[key] for r in rs if r.get(key) is not None]
            return statistics.median(vals) if vals else None

        news = [r["news2"] for r in rs if r.get("news2") is not None]
        cols = [_colour_code(r.get("colour")) for r in rs if r.get("colour")]
        points.append({
            "hr": _u8(med("hr")),
            "spo2": _u8(med("spo2")),
            "rr": _u8(med("rr")),
            "temp": _u8(med("temp"), 10, 25.0),          # 25.0 .. 50.4 degC in 0.1 steps
            "news2": _u8(max(news) if news else None),
            "colour": max(cols) if cols else 0,
            "sbp_ppg": _u8(med("sbp_ppg")),
            "dbp_ppg": _u8(med("dbp_ppg")),
        })
    return Trend(start=start, step_s=step, points=points)


# ------------------------------------------------------------------ packing --
def pack(record: dict[str, Any], trend: Trend, include_name: bool = True) -> bytes:
    if trend.step_s > 65535 or len(trend.points) > 255:
        raise PayloadError("trend too long")
    start = trend.start or int(_ts(record.get("printed_at") or datetime.now(timezone.utc)))
    printed = _ts(record["printed_at"]) if record.get("printed_at") else start
    flags = (bool(record.get("simulated")) | bool(record.get("on_oxygen")) << 1
             | bool(record.get("handed_over")) << 2)
    colours = (_colour_code(record.get("colour")) | _colour_code(record.get("worst_colour")) << 2
               | _colour_code(record.get("start_colour")) << 4)
    acvpu = record.get("acvpu")
    acvpu_code = ACVPU.index(acvpu[0].upper()) if acvpu else MISSING
    alerts = 0
    for name in record.get("alerts") or []:
        if name not in ALERTS:
            raise PayloadError(f"unknown alert {name!r}; known: {', '.join(ALERTS)}")
        alerts |= 1 << ALERTS.index(name)

    out = bytearray()
    out += struct.pack(">BBIHBh", VERSION, flags, start, trend.step_s, len(trend.points),
                       _ticks(printed - start))
    out += struct.pack(">BBBBBBBH",
                       _u8(record.get("age")), SEXES.get(record.get("sex"), 0), acvpu_code,
                       _u8(record.get("pain")), colours, _u8(record.get("news2")),
                       int(record.get("news2_measured") or 0), alerts)

    cuffs = sorted(record.get("cuffs") or [], key=lambda c: _ts(c["t"]))[-12:]
    out += bytes([len(cuffs)])
    for c in cuffs:
        out += struct.pack(">hBB", _ticks(_ts(c["t"]) - start), _u8(c.get("sys")), _u8(c.get("dia")))

    for key, limit in TEXT_LIMITS.items():
        out += _text(record.get(key), limit)

    for key in SERIES:
        prev = 0
        for p in trend.points:
            out.append((p[key] - prev) % 256)
            prev = p[key]

    has_ppg = any(p.get(k, MISSING) != MISSING for p in trend.points for k in PPG_BP)
    name = (record.get("name") or "").strip() if include_name else ""
    if name or has_ppg:
        out += _text(name, NAME_LIMIT)
    if has_ppg:
        block = bytearray()
        for key in PPG_BP:
            prev = 0
            for p in trend.points:
                value = p.get(key, MISSING)
                block.append((value - prev) % 256)
                prev = value
        out += struct.pack(">BH", TAG_PPG_BP, len(block)) + block

    out += struct.pack(">I", zlib.crc32(out))
    return bytes(out)


def unpack(data: bytes) -> dict[str, Any]:
    """Inverse of pack(). Used by the tests; docs/decode.js is the same logic in JS."""
    if len(data) < 5 or zlib.crc32(data[:-4]) != struct.unpack(">I", data[-4:])[0]:
        raise PayloadError("checksum mismatch")
    if data[0] != VERSION:
        raise PayloadError(f"unsupported version {data[0]}")
    pos = 0

    def take(fmt):
        nonlocal pos
        values = struct.unpack_from(fmt, data, pos)
        pos += struct.calcsize(fmt)
        return values

    _, flags, start, step, n, printed = take(">BBIHBh")
    age, sex, acvpu, pain, colours, news2, measured, alerts = take(">BBBBBBBH")
    cuffs = []
    for _ in range(take(">B")[0]):
        t, s, d = take(">hBB")
        cuffs.append({"t": start + t * 10, "sys": None if s == MISSING else s,
                      "dia": None if d == MISSING else d})
    texts = {}
    for key in TEXT_LIMITS:
        (length,) = take(">B")
        texts[key] = data[pos:pos + length].decode("utf-8")
        pos += length
    series = {}
    for key in SERIES:
        values, prev = [], 0
        for b in data[pos:pos + n]:
            prev = (prev + b) % 256
            values.append(prev)
        series[key] = values
        pos += n
    name = None
    end = len(data) - 4
    if pos < end:                                 # optional tail: name, then sections
        (length,) = take(">B")
        name = data[pos:pos + length].decode("utf-8") or None
        pos += length
    for key in PPG_BP:
        series[key] = [MISSING] * n
    while pos + 3 <= end:
        tag, length = take(">BH")
        block = data[pos:pos + length]
        pos += length
        if tag == TAG_PPG_BP and length == 2 * n:
            for j, key in enumerate(PPG_BP):
                prev = 0
                for i, b in enumerate(block[j * n:(j + 1) * n]):
                    prev = (prev + b) % 256
                    series[key][i] = prev

    def opt(v, scale=1.0, offset=0.0):
        return None if v == MISSING else round(v / scale + offset, 1)

    return {
        "simulated": bool(flags & 1), "on_oxygen": bool(flags & 2), "handed_over": bool(flags & 4),
        "start": start, "step_s": step, "printed_at": start + printed * 10,
        "age": opt(age), "sex": SEX_NAMES.get(sex), "acvpu": None if acvpu == MISSING else ACVPU[acvpu],
        "pain": opt(pain), "colour": COLOUR_NAMES[colours & 3], "worst_colour": COLOUR_NAMES[colours >> 2 & 3],
        "start_colour": COLOUR_NAMES[colours >> 4 & 3], "news2": opt(news2), "news2_measured": measured,
        "alerts": [name for i, name in enumerate(ALERTS) if alerts >> i & 1],
        "cuffs": cuffs, **texts, "name": name,
        "points": [{
            "t": start + i * step,
            "hr": opt(series["hr"][i]), "spo2": opt(series["spo2"][i]), "rr": opt(series["rr"][i]),
            "temp": opt(series["temp"][i], 10, 25.0), "news2": opt(series["news2"][i]),
            "colour": COLOUR_NAMES[series["colour"][i] & 3],
            "sbp_ppg": opt(series["sbp_ppg"][i]), "dbp_ppg": opt(series["dbp_ppg"][i]),
        } for i in range(n)],
    }


# --------------------------------------------------------------- text form --
def compress(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE: what DecompressionStream('deflate-raw') reads
    return c.compress(data) + c.flush()


def decompress(data: bytes) -> bytes:
    return zlib.decompress(data, -15)


def to_digits(blob: bytes) -> str:
    """Bytes -> decimal digits. The leading 0x01 keeps leading zero bytes."""
    return str(int.from_bytes(b"\x01" + blob, "big"))


def from_digits(digits: str) -> bytes:
    if not digits.isdigit():
        raise PayloadError("fragment must be digits only")
    number = int(digits)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    if not raw or raw[0] != 1:
        raise PayloadError("bad digit header")
    return raw[1:]


def encode(record: dict[str, Any], trend: Trend | None = None, include_name: bool = True) -> str:
    trend = trend or bucket(record.get("readings") or [])
    return to_digits(compress(pack(record, trend, include_name)))


def decode(digits: str) -> dict[str, Any]:
    return unpack(decompress(from_digits(digits)))
