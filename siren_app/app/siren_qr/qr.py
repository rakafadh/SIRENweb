"""Viewer URL and QR code for one patient.

The QR holds two segments: the base URL in byte mode and the payload digits
in numeric mode. Phone scanners join them back into one URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import qrcode
from qrcode.util import MODE_8BIT_BYTE, MODE_NUMBER, QRData

from . import payload as P

# 58 mm printer, 384 dots. Versions up to 12 print at 5 dots (0.63 mm) per
# module; 13 to 15 at 4 dots (0.50 mm). 15 keeps one trend point per minute
# with the PPG blood-pressure series. If printed labels scan poorly, pass
# max_version=12 (CLI --max-version 12): the trend step widens instead.
MAX_VERSION = 15
ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_M


@dataclass
class QrResult:
    url: str
    qr: qrcode.QRCode
    step_s: int
    points: int

    @property
    def version(self) -> int:
        return self.qr.version


def _normalise_base(base_url: str) -> str:
    base = base_url.split("#", 1)[0]
    if not base.startswith(("https://", "http://")):
        raise ValueError("base URL must start with https:// (or http:// for local testing)")
    return base


def build_qr(base: str, digits: str) -> qrcode.QRCode:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECTION, border=4, box_size=1)
    qr.add_data(QRData(base + "#", MODE_8BIT_BYTE))
    qr.add_data(QRData(digits, MODE_NUMBER))
    qr.make(fit=True)
    return qr


def make(record: dict[str, Any], base_url: str, max_version: int = MAX_VERSION,
         include_name: bool = True) -> QrResult:
    """Fit the trend into the QR with the finest step that fits.

    Start at one point per minute (or wider for transports over MAX_POINTS
    minutes), then widen one minute at a time: 1, 2, 3, 4 ... minutes.
    """
    base = _normalise_base(base_url)
    readings = record.get("readings") or []
    first = P.bucket(readings)
    step = first.step_s
    while True:
        trend = first if step == first.step_s else P.bucket(readings, step_s=step)
        digits = P.encode(record, trend, include_name)
        qr = build_qr(base, digits)
        if qr.version <= max_version:
            return QrResult(f"{base}#{digits}", qr, trend.step_s, len(trend.points))
        if len(trend.points) <= 2 or step >= 60 * 60:
            raise P.PayloadError("record does not fit in the QR even at a coarse step; shorten the text fields")
        step += 60
