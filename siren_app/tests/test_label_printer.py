import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import label_printer as lp
from app.services.serial_hub import SerialHub

DETAIL = {
    "patient": {
        "code": "SRN-261019-004", "name": "Rina Marlina", "display_name": "Rina Marlina", "age": 67, "sex": "Perempuan",
        "status": "ACTIVE",
        "complaint": "Jatuh dari motor, nyeri paha kanan", "systolic": 96.0, "diastolic": 60.0,
        "bp_cuff_at": "2026-10-19T05:10:00+00:00", "acvpu": "voice", "on_oxygen": True, "pain": 7,
        "created_at": "2026-10-19T05:02:00+00:00", "handed_over_at": None,
        "destination": "RSUD Kota Depok", "ambulance_id": "SIREN-01",
    },
    "latest_triage": {"colour": "MERAH", "news2_total": 9, "news2_measured": 7, "source": "NEWS2"},
    "first_triage": {"start_colour": "KUNING", "start_reason": "nyeri 7/10"},
    "worst_colour": "MERAH",
    "timeline": [
        {"captured_at": "2026-10-19T05:02:10+00:00", "hr": 96, "spo2": 97, "rr": None, "temp": 36.8,
         "sources": {"hr": "SENSOR"}, "bp_est_sbp": None, "bp_est_dbp": None, "bp_est_status": "COLLECTING",
         "triage": {"colour": "KUNING", "news2_total": 3}},
        {"captured_at": "2026-10-19T05:20:00+00:00", "hr": 110, "spo2": 93, "rr": 22, "temp": 36.6,
         "sources": {"hr": "SENSOR"}, "bp_est_sbp": 120.0, "bp_est_dbp": 76.0, "bp_est_status": "OK",
         "triage": {"colour": "KUNING", "news2_total": 6}},
        {"captured_at": "2026-10-19T05:35:00+00:00", "hr": 118, "spo2": 92, "rr": 24, "temp": 36.4,
         "sources": {"hr": "SENSOR"}, "bp_est_sbp": 112.0, "bp_est_dbp": 71.0, "bp_est_status": "OK",
         "triage": {"colour": "MERAH", "news2_total": 9}},
        {"captured_at": "2026-10-19T05:37:20+00:00", "hr": 118, "spo2": 92, "rr": 24, "temp": 36.4,
         "sources": {"hr": "SENSOR"}, "bp_est_sbp": None, "bp_est_dbp": None, "bp_est_status": "POOR_SIGNAL",
         "triage": {"colour": "MERAH", "news2_total": 9}},
    ],
    "cuffs": [{"at": "2026-10-19T05:02:00+00:00", "systolic": 128.0, "diastolic": 80.0},
              {"at": "2026-10-19T05:10:00+00:00", "systolic": 96.0, "diastolic": 60.0}],
    "alerts": [{"alert": "shock index 1.23, curiga perdarahan", "first_seen": "2026-10-19T05:10:00+00:00"}],
    "journey": {
        "start": "2026-10-19T05:02:00+00:00", "end": "2026-10-19T05:37:30+00:00", "ended": True,
        "duration_s": 2130.0,
        "colours": {"MERAH": {"episodes": 1, "seconds": 380.0}, "KUNING": {"episodes": 2, "seconds": 860.0},
                    "HIJAU": {"episodes": 1, "seconds": 890.0}},
        "pickup": {
            "triage": {"at": "2026-10-19T05:02:05+00:00", "colour": "KUNING", "news2_total": 3, "news2_measured": 6},
            "vitals": {"hr": {"value": 96.0}, "spo2": {"value": 97.0}, "rr": None, "temp": {"value": 36.8}},
            "acvpu": "alert", "on_oxygen": False, "pain": 7, "cuff": None,
        },
        "final": {
            "triage": {"at": "2026-10-19T05:37:20+00:00", "colour": "MERAH", "news2_total": 9, "news2_measured": 7},
            "vitals": {"hr": {"value": 118.0}, "spo2": {"value": 92.0}, "rr": {"value": 24.0}, "temp": {"value": 36.4}},
            "acvpu": "voice", "on_oxygen": True, "pain": 7, "cuff": {"systolic": 96.0, "diastolic": 60.0},
        },
    },
}


def cfg(**kwargs):
    base = {"PRINTER_BACKEND": "none", "PRINTER_DEVICE": "", "PRINTER_BAUD": 9600, "PRINTER_WIDTH_PX": 384}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_label_text_carries_the_essentials():
    facts = lp.label_facts(DETAIL)
    assert facts["colour"] == "MERAH" and facts["news"] == "9 (7/7)"
    assert facts["name"] == "Rina Marlina" and facts["who"] == "67 th, P"
    assert facts["cuff"].startswith("96/60 (")
    assert facts["ppg"].startswith("112/71 (")          # newest estimate the model reported OK
    assert facts["destination"] == "RSUD Kota Depok"


def test_unnamed_patient_prints_tanpa_identitas():
    unnamed = {**DETAIL, "patient": {**DETAIL["patient"], "name": None, "display_name": None}}
    assert lp.label_facts(unnamed)["name"] == "Tanpa identitas"


def test_ppg_line_waits_for_a_valid_estimate():
    no_ppg = {**DETAIL, "timeline": [{**r, "bp_est_status": "COLLECTING"} for r in DETAIL["timeline"]]}
    assert lp.label_facts(no_ppg)["ppg"] == "belum ada"


def test_simulated_data_is_marked_on_label():
    simulated = {**DETAIL, "timeline": [{**r, "sources": {"hr": "MOCK"}} for r in DETAIL["timeline"]]}
    assert lp.label_facts(simulated)["mock"] is True


def test_label_fits_paper_width():
    image = lp.render_label(DETAIL, 384)
    assert image.width == 384
    assert 300 < image.height < 1500


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
