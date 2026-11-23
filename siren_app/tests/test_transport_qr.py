"""Label QR = link to the SIREN QR viewer (app/siren_qr, from thaniyuck/siren).

These tests read the QR back off the printed label image and decode it with
the viewer's own JavaScript decoder, so a drift between SIRENweb, the encoder
and the viewer fails here rather than on a nurse's phone.
"""

import copy
import json
from datetime import datetime, timedelta
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.services import label_printer as lp
from app.services.transport_record import transport_record
from app.siren_qr import payload as P

from test_label_printer import DETAIL

VIEWER = Path(__file__).parent / "qr_viewer"


def cfg(**kwargs):
    base = {"PRINTER_BACKEND": "none", "PRINTER_DEVICE": "", "PRINTER_BAUD": 9600, "PRINTER_WIDTH_PX": 384,
            "QR_BASE_URL": "https://thaniyuck.github.io/siren/", "QR_MAX_VERSION": 15, "QR_INCLUDE_NAME": True}
    base.update(kwargs)
    return SimpleNamespace(**base)


def digits_of(url: str) -> str:
    return url.split("#", 1)[1]


def scan(image) -> str:
    cv2 = pytest.importorskip("cv2")
    data = cv2.QRCodeDetectorAruco().detectAndDecode(np.array(image.convert("L")))[0]
    assert data, "QR on the label did not decode"
    return data


def js_decode(digits: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    out = subprocess.run(["node", str(VIEWER / "decode_cli.mjs"), digits],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


# --------------------------------------------------------------- record --
def test_record_maps_the_patient_detail():
    rec = transport_record(DETAIL)
    assert rec["code"] == "SRN-261019-004" and rec["name"] == "Rina Marlina"
    assert (rec["age"], rec["sex"], rec["unit"]) == (67, "P", "SIREN-01")
    assert (rec["colour"], rec["worst_colour"], rec["start_colour"]) == ("MERAH", "MERAH", "KUNING")
    assert (rec["news2"], rec["news2_measured"], rec["acvpu"], rec["pain"]) == (9, 7, "voice", 7)
    assert rec["on_oxygen"] is True and rec["simulated"] is False and rec["handed_over"] is False
    assert [(c["sys"], c["dia"]) for c in rec["cuffs"]] == [(128.0, 80.0), (96.0, 60.0)]
    assert len(rec["readings"]) == 4
    # PPG estimate only where the model said OK
    assert [r["sbp_ppg"] for r in rec["readings"]] == [None, 120.0, 112.0, None]
    assert [r["colour"] for r in rec["readings"]] == ["KUNING", "KUNING", "MERAH", "MERAH"]


def test_alerts_use_the_viewer_codes():
    # shock index (triage), SpO2 <= 91? no (92); cuff 96 > 90; ACVPU A -> V during the trip
    assert transport_record(DETAIL)["alerts"] == ["shock_index", "new_confusion"]
    worse = copy.deepcopy(DETAIL)
    worse["timeline"][-1].update(spo2=90, rr=26, hr=135)
    worse["cuffs"].append({"at": "2026-10-19T05:30:00+00:00", "systolic": 88.0, "diastolic": 50.0})
    worse["alerts"].append({"alert": "NEWS2 naik 3 dalam 60 detik"})
    assert transport_record(worse)["alerts"] == ["shock_index", "news2_rise", "spo2_low", "rr_high", "hr_high",
                                                 "sbp_low", "new_confusion"]


def test_unknown_colours_are_left_out():
    rec = transport_record(lp.sample_detail("SIREN-01"))   # "Cetak uji" uses colour "UJI"
    assert rec["colour"] is None and rec["readings"] == []


# ------------------------------------------------------------- QR round trip --
def test_label_qr_opens_the_viewer_with_this_patient():
    result = lp.label_qr(DETAIL, cfg())
    url = scan(lp.render_label(DETAIL, 384, result))
    assert url == result.url
    assert url.startswith("https://thaniyuck.github.io/siren/#")
    data = P.decode(digits_of(url))
    assert data["code"] == "SRN-261019-004" and data["name"] == "Rina Marlina"
    assert (data["colour"], data["worst_colour"], data["news2"]) == ("MERAH", "MERAH", 9)
    assert [(c["sys"], c["dia"]) for c in data["cuffs"]] == [(128, 80), (96, 60)]
    assert data["step_s"] == result.step_s == 60 and len(data["points"]) == result.points == 36


def test_viewer_decoder_agrees(tmp_path):
    result = lp.label_qr(DETAIL, cfg())
    digits = digits_of(result.url)
    js, py = js_decode(digits), P.decode(digits)
    assert js.pop("version") == 1
    assert js == json.loads(json.dumps(py))


def test_viewer_rejects_a_damaged_scan():
    digits = digits_of(lp.label_qr(DETAIL, cfg()).url)
    broken = digits[:-3] + ("000" if not digits.endswith("000") else "111")
    assert js_decode(broken)["error"] in {"checksum", "format"}


def test_name_can_stay_on_paper_only():
    result = lp.label_qr(DETAIL, cfg(QR_INCLUDE_NAME=False))
    assert P.decode(digits_of(result.url))["name"] is None
    assert "Rina Marlina" in json.dumps(lp.label_facts(DETAIL))   # still printed


def test_base_url_is_configurable():
    url = lp.label_qr(DETAIL, cfg(QR_BASE_URL="http://localhost:8000/")).url
    assert url.startswith("http://localhost:8000/#")


def test_label_prints_even_if_the_qr_cannot_be_built():
    image = lp.render_label(DETAIL, 384, cfg=cfg(QR_BASE_URL="ftp://nope"))   # rejected by the encoder
    assert image.width == 384 and image.height < 600


def test_long_transport_widens_the_step_and_stays_small():
    long = copy.deepcopy(DETAIL)
    base = DETAIL["timeline"][1]
    t0 = datetime.fromisoformat("2026-10-19T05:02:00+00:00")
    long["timeline"] = [{**base, "captured_at": (t0 + timedelta(seconds=10 * i)).isoformat(), "hr": 90 + i % 30}
                        for i in range(6 * 150)]                        # 150 minutes of 10 s snapshots
    result = lp.label_qr(long, cfg())
    assert result.version <= settings.QR_MAX_VERSION
    assert result.step_s >= 180 and result.points <= P.MAX_POINTS
    assert scan(lp.render_label(long, 384, result)) == result.url


# ------------------------------------------------------------------ label size --
def test_label_is_not_longer_than_the_text_qr_label():
    # The text-QR label measured 718 to 776 dots for these cases (Nov 2026).
    worst = copy.deepcopy(DETAIL)
    worst["patient"].update(name="Muhammad Rizky Pratama Saputra Wijaya", display_name="Muhammad Rizky Pratama Saputra Wijaya",
                            destination="Rumah Sakit Umum Pusat Nasional Dr. Cipto Mangunkusumo")
    worst["timeline"] = [{**r, "sources": {"hr": "MOCK"}} for r in worst["timeline"]]   # adds the SIMULASI line
    for detail in (DETAIL, worst, lp.sample_detail("SIREN-01")):
        assert lp.render_label(detail, 384, cfg=cfg()).height <= 776


# --------------------------------------------------------------------- API --
def test_qr_endpoint(client):
    patient = client.post("/api/patients", json={"acvpu": "alert", "name": "Rina", "systolic": 120, "diastolic": 80}).json()["patient"]
    info = client.get(f"/api/patients/{patient['id']}/qr").json()
    assert info["url"].startswith(settings.QR_BASE_URL + "#")
    assert info["version"] >= 1 and info["module_mm"] > 0
    data = P.decode(digits_of(info["url"]))
    assert data["name"] == "Rina" and [(c["sys"], c["dia"]) for c in data["cuffs"]] == [(120, 80)]
