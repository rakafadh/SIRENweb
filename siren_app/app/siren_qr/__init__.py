"""SIREN QR: pack a transport record into a viewer URL and print it on a 58 mm label."""

from .payload import ALERTS, PayloadError, decode, encode
from .qr import MAX_VERSION, QrResult, make

__all__ = ["ALERTS", "MAX_VERSION", "PayloadError", "QrResult", "decode", "encode", "make"]
__version__ = "1.0.0"
