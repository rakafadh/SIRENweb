"""Handover label for a 58 mm thermal printer: readable text plus a QR summary.

The label is drawn as a 1-bit image and sent with the ESC/POS raster command
(GS v 0), which nearly every 58 mm receipt printer understands, whatever its
fonts or code page. The QR holds plain text so any phone camera can read it
without an app or a network.

Backends (PRINTER_BACKEND):
  none    printing disabled
  file    write the label as PNG to PRINTER_DEVICE (a folder), for testing
  escpos  send to PRINTER_DEVICE: /dev/usb/lp0 (USB printer class),
          /dev/ttyUSB*, /dev/rfcomm0 (Bluetooth SPP), COM5, or socket://ip:9100
"""

from __future__ import annotations

import io
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import qrcode
import serial
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.config import Settings, settings

log = logging.getLogger("siren.printer")

ACVPU_SHORT = {"alert": "A", "confusion": "C", "voice": "V", "pain": "P", "unresponsive": "U"}
QR_TEXT_LIMIT = 280     # bytes; version 12 at error correction M holds 287
BAND_ROWS = 128         # rows per raster command; small printers have small buffers

_FONT_CANDIDATES = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"],
}


class PrinterError(RuntimeError):
    pass


def _font(size: int, bold: bool = False):
    for path in _FONT_CANDIDATES[bold]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _when(iso: str | None, fmt: str = "%H:%M") -> str:
    return datetime.fromisoformat(iso).astimezone().strftime(fmt) if iso else "-"


def _num(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _clip(text: str | None, limit: int) -> str:
    text = (text or "-").strip()
    return text if len(text) <= limit else text[: limit - 2] + ".."


# ------------------------------------------------------------------ content --
def label_facts(detail: dict[str, Any]) -> dict[str, Any]:
    patient = detail["patient"]
    latest = detail["latest_triage"] or {}
    first = detail["first_triage"] or {}
    last = detail["timeline"][-1] if detail["timeline"] else {}
    sex = {"Laki-laki": "L", "Perempuan": "P"}.get(patient["sex"], "kelamin -")
    age = f"{patient['age']} th" if patient["age"] is not None else "usia -"
    news = latest.get("news2_total")
    return {
        "code": patient["code"],
        "colour": latest.get("colour") or "BELUM ADA DATA",
        "news": "-" if news is None else f"{news} ({latest.get('news2_measured', 0)}/7)",
        "source": latest.get("source") or "-",
        "start": f"{first.get('start_colour') or '-'} - {first.get('start_reason') or '-'}",
        "worst": detail.get("worst_colour") or "-",
        "name": patient["display_name"],
        "who": f"{age}, {sex}",
        "complaint": patient["complaint"],
        "vitals": (f"N {_num(last.get('hr'))}  SpO2 {_num(last.get('spo2'))}  "
                   f"RR {_num(last.get('rr'))}  S {_num(last.get('temp'), 1)}"),
        "vitals_short": (f"N{_num(last.get('hr'))} SpO2 {_num(last.get('spo2'))} "
                         f"RR{_num(last.get('rr'))} S{_num(last.get('temp'), 1)}"),
        "vitals_at": _when(last.get("captured_at")),
        "cuff": (f"{_num(patient['systolic'])}/{_num(patient['diastolic'])} ({_when(patient['bp_cuff_at'])})"
                 if patient["systolic"] is not None else "belum diukur"),
        "status": (f"Kesadaran {ACVPU_SHORT.get(patient['acvpu'], '-')}, O2 {'ya' if patient['on_oxygen'] else 'tidak'}, "
                   f"nyeri {patient['pain'] if patient['pain'] is not None else '-'}/10"),
        "status_short": (f"ACVPU {ACVPU_SHORT.get(patient['acvpu'], '-')} O2 {'ya' if patient['on_oxygen'] else 'tdk'} "
                         f"nyeri {patient['pain'] if patient['pain'] is not None else '-'}"),
        "alerts": [a["alert"] for a in detail["alerts"]],
        "started": _when(patient["created_at"]),
        "handed": _when(patient["handed_over_at"]) if patient["handed_over_at"] else None,
        "destination": patient["destination"],
        "unit": patient["ambulance_id"],
        "mock": any((r.get("sources") or {}).get("hr") == "MOCK" for r in detail["timeline"]),
    }


def qr_text(facts: dict[str, Any]) -> str:
    """Plain text summary, kept short so the QR stays at version 12 or below.

    At 384 dots that leaves at least 5 dots (0.6 mm) per module, which phone
    cameras read reliably from label distance. More text means smaller modules.
    """
    # Most important first: if anything is cut, it is the tail.
    source = "" if facts["source"] == "NEWS2" else f" ({facts['source']})"
    lines = [
        f"SIREN {facts['code']}" + (" SIMULASI" if facts["mock"] else ""),
        f"{facts['colour']}{source} NEWS2 {facts['news']}",
        f"{_clip(facts['name'], 30)}, {facts['who']}",
        f"{facts['vitals_at']} {facts['vitals_short']}",
        f"TD {facts['cuff']}; {facts['status_short']}",
        f"Terberat {facts['worst']}; START {_clip(facts['start'], 34)}",
    ]
    if facts["alerts"]:
        lines.append("! " + "; ".join(_clip(a, 30) for a in facts["alerts"][:2]))
    lines += [
        f"Mulai {facts['started']}" + (f" serah {facts['handed']}" if facts["handed"] else "")
        + f"; {facts['unit']}; RS {_clip(facts['destination'], 26)}",
        f"Keluhan: {_clip(facts['complaint'], 44)}",
    ]
    text = "\n".join(lines).encode("ascii", "replace").decode("ascii")
    return text if len(text) <= QR_TEXT_LIMIT else text[: QR_TEXT_LIMIT - 2] + ".."


def sample_detail(ambulance_id: str) -> dict[str, Any]:
    """A clearly fake patient for 'Cetak uji' on the Sensor page."""
    now = datetime.now().astimezone().isoformat()
    return {
        "patient": {
            "code": "CETAK-UJI", "display_name": "CETAK UJI - bukan pasien", "age": None, "sex": None,
            "complaint": "Uji printer", "systolic": None, "diastolic": None, "bp_cuff_at": None,
            "acvpu": "alert", "on_oxygen": False, "pain": None, "created_at": now, "handed_over_at": None,
            "destination": None, "ambulance_id": ambulance_id,
        },
        "latest_triage": {"colour": "UJI", "news2_total": None, "news2_measured": 0, "source": "uji"},
        "first_triage": None,
        "worst_colour": None,
        "timeline": [],
        "alerts": [],
    }


# ------------------------------------------------------------------- render --
def render_label(detail: dict[str, Any], width: int) -> Image.Image:
    facts = label_facts(detail)
    margin = 8
    inner = width - 2 * margin
    regular, small, bold = _font(20), _font(17), _font(22, bold=True)
    big = _font(46, bold=True)

    canvas = Image.new("L", (width, 2000), 255)
    draw = ImageDraw.Draw(canvas)
    y = 4

    def text(line: str, font, gap: int = 4):
        nonlocal y
        for chunk in _wrap(draw, line, font, inner):
            draw.text((margin, y), chunk, font=font, fill=0)
            y += font.size + gap

    text(f"SIREN  {facts['code']}", bold)
    if facts["mock"]:
        text("SIMULASI - bukan data pasien", small)

    # Category as a black block: the one thing readable from across the room
    block_h = big.size + 18
    draw.rectangle([margin, y, width - margin, y + block_h], fill=0)
    box = draw.textbbox((0, 0), facts["colour"], font=big)
    draw.text(((width - (box[2] - box[0])) // 2, y + 6), facts["colour"], font=big, fill=255)
    y += block_h + 8

    text(f"NEWS2 {facts['news']}", regular)
    text(facts["name"], bold)
    text(facts["who"], regular)
    text(f"Vital {facts['vitals_at']}: {facts['vitals']}", small)
    text(f"TD manset {facts['cuff']}", small)
    text(facts["status"], small)
    text(f"Mulai {facts['started']}  Cetak {datetime.now().astimezone():%H:%M}", small)
    if facts["destination"]:
        text(f"Tujuan: {_clip(facts['destination'], 60)}", small)

    y += 6
    # ISO/IEC 18004 asks for a 4-module quiet zone; phone readers struggle with less
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=1)
    qr.add_data(qr_text(facts))
    qr.make(fit=True)
    matrix = qr.make_image(fill_color="black", back_color="white").convert("L")
    scale = max(1, width // matrix.width)   # quiet zone may use the side margins
    matrix = matrix.resize((matrix.width * scale, matrix.height * scale), Image.NEAREST)
    canvas.paste(matrix, ((width - matrix.width) // 2, y))
    y += matrix.height + 4

    text("Pindai QR untuk ringkasan lengkap.", small, gap=2)
    text("Alat bantu, bukan pengganti penilaian klinis.", small, gap=2)
    return canvas.crop((0, 0, width, y + 6))


def _wrap(draw: ImageDraw.ImageDraw, line: str, font, width: int) -> list[str]:
    words, out, current = line.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not current:
            current = trial
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out or [""]


def escpos_raster(image: Image.Image) -> bytes:
    """ESC/POS bytes: initialise, raster bands, feed. No cut; pocket printers have no cutter."""
    mono = ImageOps.invert(image.convert("L")).convert("1", dither=Image.Dither.NONE)
    if mono.width % 8:
        padded = Image.new("1", (mono.width + 8 - mono.width % 8, mono.height), 0)
        padded.paste(mono, (0, 0))
        mono = padded
    row_bytes = mono.width // 8
    out = bytearray(b"\x1b\x40")   # ESC @
    for top in range(0, mono.height, BAND_ROWS):
        band = mono.crop((0, top, mono.width, min(top + BAND_ROWS, mono.height)))
        out += b"\x1d\x76\x30\x00"  # GS v 0, normal density
        out += bytes([row_bytes & 0xFF, row_bytes >> 8, band.height & 0xFF, band.height >> 8])
        out += band.tobytes()
    out += b"\x1b\x64\x04"          # ESC d 4: feed past the tear bar
    return bytes(out)


# ---------------------------------------------------------------- transport --
class LabelPrinter:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return self.cfg.PRINTER_BACKEND

    def status(self) -> dict[str, Any]:
        device = self.cfg.PRINTER_DEVICE
        reachable = None
        if self.backend == "escpos" and device and device.startswith("/dev/"):
            reachable = Path(device).exists()
        return {
            "backend": self.backend,
            "device": device or None,
            "reachable": reachable,
            "width_px": self.cfg.PRINTER_WIDTH_PX,
        }

    def render_png(self, detail: dict[str, Any]) -> bytes:
        buffer = io.BytesIO()
        render_label(detail, self.cfg.PRINTER_WIDTH_PX).save(buffer, format="PNG")
        return buffer.getvalue()

    def print_label(self, detail: dict[str, Any]) -> str:
        """Print, or raise PrinterError with a message fit for the screen."""
        if self.backend == "none":
            raise PrinterError("Printer belum diatur. Isi PRINTER_BACKEND dan PRINTER_DEVICE di .env.")
        image = render_label(detail, self.cfg.PRINTER_WIDTH_PX)
        with self._lock:
            if self.backend == "file":
                folder = Path(self.cfg.PRINTER_DEVICE or "prints")
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"label-{detail['patient']['code']}-{datetime.now():%H%M%S}.png"
                image.save(path)
                return f"Label disimpan sebagai {path.name} (mode file, tanpa printer)."
            if self.backend == "escpos":
                self._send(escpos_raster(image))
                return "Label terkirim ke printer."
        raise PrinterError(f"PRINTER_BACKEND tidak dikenal: {self.backend}")

    def _send(self, payload: bytes) -> None:
        device = self.cfg.PRINTER_DEVICE
        if not device:
            raise PrinterError("PRINTER_DEVICE kosong.")
        try:
            if device.startswith("/dev/usb/lp") or device.startswith("/dev/lp"):
                # USB printer class: a plain character device
                with open(device, "wb", buffering=0) as handle:
                    handle.write(payload)
            else:
                with serial.serial_for_url(device, self.cfg.PRINTER_BAUD, timeout=2, write_timeout=15) as port:
                    port.write(payload)
                    port.flush()
        except FileNotFoundError:
            raise PrinterError(f"Printer tidak ditemukan di {device}. Periksa kabel, daya, atau pairing Bluetooth.") from None
        except PermissionError:
            raise PrinterError(f"Tidak ada izin menulis ke {device}. User perlu masuk grup lp/dialout.") from None
        except serial.SerialTimeoutException:
            raise PrinterError("Printer tidak merespons (timeout). Periksa kertas dan daya printer.") from None
        except (serial.SerialException, OSError) as exc:
            log.exception("Printing failed")
            raise PrinterError(f"Gagal mengirim ke printer: {exc}") from None
