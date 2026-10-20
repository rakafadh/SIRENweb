import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.services import label_printer as lp
from app.services.serial_hub import SerialHub

DETAIL = {
    "patient": {
        "code": "SRN-261019-004", "display_name": "Rina Marlina", "age": 67, "sex": "Perempuan",
        "complaint": "Jatuh dari motor, nyeri paha kanan", "systolic": 96.0, "diastolic": 60.0,
        "bp_cuff_at": "2026-10-19T05:10:00+00:00", "acvpu": "voice", "on_oxygen": True, "pain": 7,
        "created_at": "2026-10-19T05:02:00+00:00", "handed_over_at": None,
        "destination": "RSUD Kota Depok", "ambulance_id": "SIREN-01",
    },
    "latest_triage": {"colour": "MERAH", "news2_total": 9, "news2_measured": 7, "source": "NEWS2"},
    "first_triage": {"start_colour": "KUNING", "start_reason": "nyeri 7/10"},
    "worst_colour": "MERAH",
    "timeline": [{"captured_at": "2026-10-19T05:12:00+00:00", "hr": 118, "spo2": 92, "rr": 24, "temp": 36.4,
                  "sources": {"hr": "SENSOR"}}],
    "alerts": [{"alert": "shock index 1.23, curiga perdarahan", "first_seen": "2026-10-19T05:10:00+00:00"}],
}


def cfg(**kwargs):
    base = {"PRINTER_BACKEND": "none", "PRINTER_DEVICE": "", "PRINTER_BAUD": 9600, "PRINTER_WIDTH_PX": 384}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_qr_text_is_readable_summary():
    text = lp.qr_text(lp.label_facts(DETAIL))
    assert len(text.encode()) <= lp.QR_TEXT_LIMIT
    assert text.isascii()
    assert text.startswith("SIREN SRN-261019-004\nMERAH NEWS2 9 (7/7)\nRina Marlina")
    assert "N118 SpO2 92 RR24 S36.4" in text
    assert "TD 96/60" in text
    assert "RS RSUD Kota Depok" in text
    assert "shock index" in text
    assert "SIMULASI" not in text


def test_long_free_text_cannot_push_out_the_essentials():
    wordy = {**DETAIL, "patient": {**DETAIL["patient"], "complaint": "x" * 400, "display_name": "y" * 150}}
    text = lp.qr_text(lp.label_facts(wordy))
    assert len(text) <= lp.QR_TEXT_LIMIT
    assert "MERAH NEWS2" in text and "TD 96/60" in text


def test_simulated_data_is_marked_on_label():
    simulated = {**DETAIL, "timeline": [{**DETAIL["timeline"][0], "sources": {"hr": "MOCK"}}]}
    assert lp.qr_text(lp.label_facts(simulated)).startswith("SIREN SRN-261019-004 SIMULASI")


def test_label_fits_paper_width():
    image = lp.render_label(DETAIL, 384)
    assert image.width == 384
    assert 300 < image.height < 1500


def test_qr_on_label_decodes():
    # Optional check: pip install opencv-python-headless. The ArUco-based
    # detector is used because OpenCV's classic one misses version 12 codes
    # that phones read without trouble.
    cv2 = pytest.importorskip("cv2")
    image = np.array(lp.render_label(DETAIL, 384).convert("L"))
    data = cv2.QRCodeDetectorAruco().detectAndDecode(image)[0]
    assert data == lp.qr_text(lp.label_facts(DETAIL))


def test_escpos_raster_layout():
    image = Image.new("L", (384, 300), 255)
    image.paste(0, (0, 0, 8, 1))   # one black byte in the top-left corner
    payload = lp.escpos_raster(image)
    assert payload[:2] == b"\x1b\x40"
    assert payload[2:10] == b"\x1d\x76\x30\x00" + bytes([48, 0, 128, 0])
    assert payload[10] == 0xFF                     # black prints as 1 bits
    assert payload[11] == 0x00
    bands = 3                                      # 128 + 128 + 44 rows
    assert len(payload) == 2 + bands * 8 + 48 * 300 + 3
    assert payload.endswith(b"\x1b\x64\x04")


def test_disabled_printer_explains_itself():
    with pytest.raises(lp.PrinterError, match="belum diatur"):
        lp.LabelPrinter(cfg()).print_label(DETAIL)


def test_file_backend_writes_png(tmp_path):
    message = lp.LabelPrinter(cfg(PRINTER_BACKEND="file", PRINTER_DEVICE=str(tmp_path))).print_label(DETAIL)
    files = list(tmp_path.glob("label-SRN-261019-004-*.png"))
    assert len(files) == 1 and "disimpan" in message
    assert Image.open(files[0]).width == 384


def test_escpos_backend_sends_bytes(monkeypatch):
    sent = io.BytesIO()

    class FakePort:
        def __init__(self, *args, **kwargs):
            self.args = args
        def write(self, data):
            sent.write(data)
        def flush(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(lp.serial, "serial_for_url", FakePort)
    printer = lp.LabelPrinter(cfg(PRINTER_BACKEND="escpos", PRINTER_DEVICE="/dev/rfcomm0"))
    assert printer.print_label(DETAIL) == "Label terkirim ke printer."
    assert sent.getvalue().startswith(b"\x1b\x40\x1d\x76\x30\x00")


def test_missing_device_is_reported(monkeypatch):
    def refuse(*args, **kwargs):
        raise lp.serial.SerialException("could not open port /dev/rfcomm0")

    monkeypatch.setattr(lp.serial, "serial_for_url", refuse)
    printer = lp.LabelPrinter(cfg(PRINTER_BACKEND="escpos", PRINTER_DEVICE="/dev/rfcomm0"))
    with pytest.raises(lp.PrinterError, match="Gagal mengirim"):
        printer.print_label(DETAIL)


def test_sensor_scan_skips_printer_port():
    hub = SerialHub(115200, ["/dev/ttyUSB0", "/dev/ttyUSB2"], lambda *a: None, lambda *a: None, exclude=["/dev/ttyUSB2"])
    assert hub.candidate_ports() == ["/dev/ttyUSB0"]


def test_print_endpoint(client, app_module, tmp_path, monkeypatch):
    patient = client.post("/api/patients", json={"acvpu": "alert", "name": "Rina"}).json()["patient"]

    response = client.post(f"/api/patients/{patient['id']}/print-label")
    assert response.status_code == 503
    assert "belum diatur" in response.json()["detail"]

    monkeypatch.setattr(app_module.label_printer, "cfg", cfg(PRINTER_BACKEND="file", PRINTER_DEVICE=str(tmp_path)))
    assert client.post(f"/api/patients/{patient['id']}/print-label").status_code == 200
    assert list(tmp_path.glob("*.png"))
    events = client.get(f"/api/patients/{patient['id']}").json()["events"]
    assert any(e["kind"] == "CETAK" for e in events)

    preview = client.get(f"/api/patients/{patient['id']}/label.png")
    assert preview.headers["content-type"] == "image/png"
    assert client.post("/api/printer/test").status_code == 200
