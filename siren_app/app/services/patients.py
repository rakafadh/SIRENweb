"""Patient records: creation, logged edits, and the shapes the API returns."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import utcnow
from app.models import Patient, PatientEvent, TriageResult, VitalReading
from app.schemas import PatientCreate, PatientUpdate

ACVPU_LABEL = {
    "alert": "Alert (sadar penuh)",
    "confusion": "Confusion (bingung baru)",
    "voice": "Voice (respons suara)",
    "pain": "Pain (respons nyeri)",
    "unresponsive": "Unresponsive (tidak responsif)",
}
FIELD_LABEL = {
    "name": "Nama", "age": "Usia", "sex": "Jenis kelamin", "complaint": "Keluhan/mekanisme",
    "comorbid": "Komorbid", "destination": "RS tujuan", "crew": "Petugas",
    "ambulatory": "Dapat berjalan", "uncontrolled_bleeding": "Perdarahan tak terkontrol",
    "limb_loss_risk": "Risiko kehilangan anggota gerak", "chest_pain": "Nyeri dada",
    "acvpu": "Kesadaran (ACVPU)", "on_oxygen": "Oksigen tambahan", "pain": "Skala nyeri",
}
START_FIELDS = ("ambulatory", "uncontrolled_bleeding", "limb_loss_risk", "chest_pain")
CUFF_FIELDS = ("systolic", "diastolic")


def local_time(value: datetime | None) -> datetime | None:
    """Device-local wall clock. Set the Orange Pi's timezone to Asia/Jakarta."""
    return None if value is None else value.astimezone()


def _fmt(field: str, value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "ya" if value else "tidak"
    if field == "acvpu":
        return ACVPU_LABEL.get(value, value)
    return str(value)


def next_code(db: Session) -> str:
    """SRN-YYMMDD-NNN, numbered per local day."""
    today = datetime.now().astimezone()
    prefix = f"SRN-{today:%y%m%d}-"
    count = db.scalar(select(func.count()).select_from(Patient).where(Patient.code.like(f"{prefix}%"))) or 0
    return f"{prefix}{count + 1:03d}"


def active_patient(db: Session) -> Patient | None:
    return db.scalars(select(Patient).where(Patient.status == "ACTIVE").order_by(Patient.created_at.desc())).first()


def create_patient(db: Session, body: PatientCreate) -> Patient:
    data = body.model_dump()
    patient = Patient(code=next_code(db), ambulance_id=settings.AMBULANCE_ID, **data)
    if body.systolic is not None:
        patient.bp_cuff_at = utcnow()
    db.add(patient)
    db.flush()

    start_items = [FIELD_LABEL[f] for f in START_FIELDS if data[f]]
    db.add(PatientEvent(
        patient_id=patient.id,
        kind="START",
        detail=(
            f"Penjemputan. Kesadaran: {_fmt('acvpu', body.acvpu)}; "
            f"kriteria: {', '.join(start_items) if start_items else 'tidak ada'}; "
            f"nyeri: {_fmt('pain', body.pain)}; oksigen: {_fmt('on_oxygen', body.on_oxygen)}"
        ),
        data={f: data[f] for f in (*START_FIELDS, "acvpu", "pain", "on_oxygen")},
    ))
    if body.systolic is not None:
        db.add(_cuff_event(patient))
    db.commit()
    return patient


def _cuff_event(patient: Patient) -> PatientEvent:
    dia = f"/{patient.diastolic:.0f}" if patient.diastolic is not None else ""
    return PatientEvent(
        patient_id=patient.id,
        kind="CUFF_BP",
        detail=f"Tekanan darah manset {patient.systolic:.0f}{dia} mmHg",
        data={"systolic": patient.systolic, "diastolic": patient.diastolic},
    )


def update_patient(db: Session, patient: Patient, body: PatientUpdate, undo: bool = False) -> list[str]:
    """Apply only the fields sent, and log each real change for the handover timeline."""
    changes = body.model_dump(exclude_unset=True)
    cuff_changed = False
    described: list[str] = []
    for field, value in changes.items():
        if getattr(patient, field) == value:
            continue
        if field in CUFF_FIELDS:
            cuff_changed = True
            setattr(patient, field, value)
            continue
        old = getattr(patient, field)
        setattr(patient, field, value)
        described.append(f"{FIELD_LABEL[field]}: {_fmt(field, old)} → {_fmt(field, value)}")

    if cuff_changed:
        if "systolic" in changes and "diastolic" not in changes:
            patient.diastolic = None   # a new systolic with the old diastolic would be a fabricated pair
        if patient.systolic is None:
            patient.bp_cuff_at = None
            db.add(PatientEvent(patient_id=patient.id, kind="CUFF_BP", detail="Tekanan darah manset dihapus", data=None))
        else:
            patient.bp_cuff_at = utcnow()
            db.add(_cuff_event(patient))
    if described:
        detail = "; ".join(described)
        db.add(PatientEvent(patient_id=patient.id, kind="INPUT", detail=f"Diurungkan: {detail}" if undo else detail, data=changes))
    db.commit()
    return described


def serialize_patient(patient: Patient) -> dict[str, Any]:
    from app.services.monitor import age_flags  # avoid an import cycle

    elderly, paediatric = age_flags(patient.age)
    return {
        "id": patient.id,
        "code": patient.code,
        "status": patient.status,
        "name": patient.name,
        "display_name": patient.name or "Tanpa identitas",
        "age": patient.age,
        "sex": patient.sex,
        "complaint": patient.complaint,
        "comorbid": patient.comorbid,
        "elderly": elderly,
        "paediatric": paediatric,
        "destination": patient.destination,
        "crew": patient.crew,
        "ambulance_id": patient.ambulance_id,
        "ambulatory": patient.ambulatory,
        "uncontrolled_bleeding": patient.uncontrolled_bleeding,
        "limb_loss_risk": patient.limb_loss_risk,
        "chest_pain": patient.chest_pain,
        "acvpu": patient.acvpu,
        "on_oxygen": patient.on_oxygen,
        "pain": patient.pain,
        "systolic": patient.systolic,
        "diastolic": patient.diastolic,
        "bp_cuff_at": patient.bp_cuff_at.isoformat() if patient.bp_cuff_at else None,
        "created_at": patient.created_at.isoformat() if patient.created_at else None,
        "handed_over_at": patient.handed_over_at.isoformat() if patient.handed_over_at else None,
    }


def serialize_reading(reading: VitalReading) -> dict[str, Any]:
    return {
        "captured_at": reading.captured_at.isoformat(),
        "hr": reading.hr, "spo2": reading.spo2, "rr": reading.rr, "temp": reading.temp,
        "pi": reading.pi, "motion_g": reading.motion_g, "sources": reading.sources,
        "systolic": reading.systolic, "diastolic": reading.diastolic,
        "bp_est_sbp": reading.bp_est_sbp, "bp_est_dbp": reading.bp_est_dbp, "bp_est_status": reading.bp_est_status,
        "quality": reading.quality,
    }


def serialize_triage(row: TriageResult) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "colour": row.colour, "raw_colour": row.raw_colour, "source": row.source,
        "news2_total": row.news2_total, "news2_measured": row.news2_measured,
        "news2_colour": row.news2_colour, "news2_parts": row.news2_parts,
        "start_colour": row.start_colour, "start_reason": row.start_reason,
        "delta": row.delta, "shock_index": row.shock_index, "alerts": row.alerts or [],
    }


def serialize_event(event: PatientEvent) -> dict[str, Any]:
    return {"created_at": event.created_at.isoformat(), "kind": event.kind, "detail": event.detail}


def patient_detail(db: Session, patient: Patient) -> dict[str, Any]:
    readings = list(patient.readings)
    triages = list(patient.triage_results)
    pairs = {t.reading_id: t for t in triages}
    timeline = []
    for reading in readings:
        tri = pairs.get(reading.id)
        timeline.append({**serialize_reading(reading), "triage": serialize_triage(tri) if tri else None})

    # "shock index 0.91", "0.93", ... are one alert, not one per snapshot.
    # Group by the text without its numbers; keep the latest wording.
    alerts_seen: dict[str, dict[str, Any]] = {}
    for tri in triages:
        for alert in tri.alerts or []:
            key = re.sub(r"\d+(?:[.,]\d+)?", "#", alert)
            when = tri.created_at.isoformat()
            entry = alerts_seen.setdefault(key, {"alert": alert, "first_seen": when, "last_seen": when, "count": 0})
            entry.update(alert=alert, last_seen=when)
            entry["count"] += 1

    unmeasured: dict[str, str] = {}
    for event in patient.events:
        if event.kind == "TIDAK_DIUKUR" and event.data:
            if event.data.get("reason"):
                unmeasured[event.data["param"]] = event.data["reason"]
            else:
                unmeasured.pop(event.data.get("param"), None)

    return {
        "patient": serialize_patient(patient),
        "unmeasured": unmeasured,
        "first_triage": serialize_triage(triages[0]) if triages else None,
        "latest_triage": serialize_triage(triages[-1]) if triages else None,
        "worst_colour": _worst([t.colour for t in triages]),
        "timeline": timeline,
        "events": [serialize_event(e) for e in patient.events],
        "alerts": list(alerts_seen.values()),
    }


def _worst(colours: list[str]) -> str | None:
    order = {"HIJAU": 0, "KUNING": 1, "MERAH": 2}
    present = [c for c in colours if c in order]
    return max(present, key=order.__getitem__) if present else None
