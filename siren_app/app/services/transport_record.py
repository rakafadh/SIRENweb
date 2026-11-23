"""Patient detail -> transport record for the label QR (app/siren_qr).

The QR is a link to the SIREN QR viewer with the whole trip packed after the
'#'. This module is the only place that knows both shapes: SIRENweb's patient
detail on one side, the record format of thaniyuck/siren on the other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.siren_qr import payload as P

SEX = {"Laki-laki": "L", "Perempuan": "P"}

# The viewer's alert codes are a fixed list (bit positions in the payload).
# Triage messages map by their opening words; the rest are vital-sign
# thresholds checked over the whole trip, as defined in payload.ALERTS.
TRIAGE_ALERTS = {"NEWS2 naik": "news2_rise", "shock index": "shock_index"}
THRESHOLDS = [
    ("spo2_low", "spo2", lambda v: v <= 91),
    ("rr_high", "rr", lambda v: v >= 25),
    ("rr_low", "rr", lambda v: v <= 8),
    ("hr_high", "hr", lambda v: v >= 131),
    ("hr_low", "hr", lambda v: v <= 40),
    ("temp_high", "temp", lambda v: v >= 39.1),
    ("temp_low", "temp", lambda v: v <= 35.0),
]


def _colour(value: str | None) -> str | None:
    return value if value in P.COLOURS else None


def _alerts(detail: dict[str, Any], cuffs: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for item in detail.get("alerts") or []:
        for prefix, code in TRIAGE_ALERTS.items():
            if item["alert"].startswith(prefix):
                found.add(code)
    for code, key, test in THRESHOLDS:
        if any(r.get(key) is not None and test(r[key]) for r in detail["timeline"]):
            found.add(code)
    if any(c["sys"] is not None and c["sys"] <= 90 for c in cuffs):
        found.add("sbp_low")
    journey = detail.get("journey") or {}
    if (journey.get("pickup") or {}).get("acvpu") == "alert" and detail["patient"].get("acvpu") not in (None, "alert"):
        found.add("new_confusion")
    return [code for code in P.ALERTS if code in found]   # payload order


def transport_record(detail: dict[str, Any], printed_at: datetime | None = None) -> dict[str, Any]:
    patient = detail["patient"]
    latest = detail.get("latest_triage") or {}
    first = detail.get("first_triage") or {}
    cuffs = [{"t": c["at"], "sys": c["systolic"], "dia": c["diastolic"]} for c in detail.get("cuffs") or []]
    readings = []
    for row in detail["timeline"]:
        tri = row.get("triage") or {}
        ppg_ok = row.get("bp_est_status") == "OK"
        readings.append({
            "t": row["captured_at"],
            "hr": row.get("hr"), "spo2": row.get("spo2"), "rr": row.get("rr"), "temp": row.get("temp"),
            # The PPG model is an estimate: only when it reports OK, never in place of the cuff
            "sbp_ppg": row.get("bp_est_sbp") if ppg_ok else None,
            "dbp_ppg": row.get("bp_est_dbp") if ppg_ok else None,
            "news2": tri.get("news2_total"), "colour": _colour(tri.get("colour")),
        })
    return {
        "code": patient.get("code"),
        "name": (patient.get("name") or "").strip() or None,
        "age": patient.get("age"),
        "sex": SEX.get(patient.get("sex")),
        "complaint": patient.get("complaint"),
        "unit": patient.get("ambulance_id"),
        "acvpu": patient.get("acvpu"),
        "on_oxygen": bool(patient.get("on_oxygen")),
        "pain": patient.get("pain"),
        "start_colour": _colour(first.get("start_colour")),
        "colour": _colour(latest.get("colour")),
        "worst_colour": _colour(detail.get("worst_colour")),
        "news2": latest.get("news2_total"),
        "news2_measured": latest.get("news2_measured") or 0,
        "alerts": _alerts(detail, cuffs),
        "simulated": any((r.get("sources") or {}).get("hr") == "MOCK" for r in detail["timeline"]),
        "handed_over": patient.get("status") == "HANDED_OVER",
        "printed_at": (printed_at or datetime.now(timezone.utc)).isoformat(),
        "cuffs": cuffs,
        "readings": readings,
    }
