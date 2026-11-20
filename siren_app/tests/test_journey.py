"""Trip summary for the handover screen and the label QR."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from app.services.patients import journey_summary

T0 = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def tri(seconds, colour, news=None):
    return NS(created_at=at(seconds), colour=colour, news2_total=news, news2_measured=7, source="NEWS2",
              start_colour="HIJAU", start_reason="dapat berjalan", shock_index=None)


def reading(seconds, **vitals):
    base = {"hr": None, "spo2": None, "rr": None, "temp": None}
    base.update(vitals)
    return NS(captured_at=at(seconds), sources={k: "SENSOR" for k in vitals}, **base)


def patient(handed=None, events=()):
    return NS(created_at=T0, handed_over_at=handed, events=list(events), acvpu="voice", on_oxygen=True, pain=5,
              systolic=95.0, diastolic=60.0, bp_cuff_at=at(600))


def test_time_and_episodes_per_colour():
    triages = [tri(0, "HIJAU"), tri(10, "HIJAU"), tri(60, "KUNING"), tri(120, "MERAH"),
               tri(300, "KUNING"), tri(400, "HIJAU"), tri(500, "KUNING")]
    j = journey_summary(patient(handed=at(600)), [], triages)
    assert j["duration_s"] == 600
    assert j["ended"] is True
    assert j["colours"]["HIJAU"] == {"episodes": 2, "seconds": 160.0}
    assert j["colours"]["KUNING"] == {"episodes": 3, "seconds": 60.0 + 100.0 + 100.0}
    assert j["colours"]["MERAH"] == {"episodes": 1, "seconds": 180.0}
    assert [s["colour"] for s in j["segments"]] == ["HIJAU", "KUNING", "MERAH", "KUNING", "HIJAU", "KUNING"]
    assert sum(c["seconds"] for c in j["colours"].values()) == j["duration_s"]


def test_active_trip_runs_to_now():
    j = journey_summary(patient(), [], [tri(0, "HIJAU")], now=at(90))
    assert j["ended"] is False and j["duration_s"] == 90
    assert j["colours"]["HIJAU"]["seconds"] == 90


def test_pickup_uses_first_values_and_final_uses_last():
    readings = [reading(0, hr=90, spo2=98), reading(25, hr=92, rr=16), reading(40, temp=36.9),
                reading(500, hr=120, spo2=91), reading(590, rr=26), reading(595, hr=125)]
    start = NS(kind="START", data={"acvpu": "alert", "on_oxygen": False, "pain": 2}, created_at=T0)
    cuff = NS(kind="CUFF_BP", data={"systolic": 130.0, "diastolic": 85.0}, created_at=at(5))
    j = journey_summary(patient(handed=at(600), events=[start, cuff]), readings, [tri(0, "HIJAU"), tri(500, "MERAH")])
    p, f = j["pickup"], j["final"]
    assert {k: (v or {}).get("value") for k, v in p["vitals"].items()} == {"hr": 90, "spo2": 98, "rr": 16, "temp": 36.9}
    assert (p["acvpu"], p["on_oxygen"], p["pain"]) == ("alert", False, 2)
    assert p["cuff"]["systolic"] == 130.0
    assert p["triage"]["colour"] == "HIJAU" and f["triage"]["colour"] == "MERAH"
    # temp was last seen 560 s before the end: too old to call it the final value
    assert {k: (v or {}).get("value") for k, v in f["vitals"].items()} == {"hr": 125, "spo2": 91, "rr": 26, "temp": None}
    assert (f["acvpu"], f["on_oxygen"], f["pain"]) == ("voice", True, 5)
    assert f["cuff"]["systolic"] == 95.0


def test_no_data_yet():
    j = journey_summary(patient(), [], [], now=at(5))
    assert j["segments"] == [] and j["pickup"]["triage"] is None
    assert all(c["episodes"] == 0 for c in j["colours"].values())
