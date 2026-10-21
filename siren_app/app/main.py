from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import errors
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.database import create_db_and_tables, get_db, utcnow
from app.models import Patient, PatientEvent, SensorEvent, TriageResult, VitalReading
from app.schemas import ManualVitals, NoteCreate, PatientCreate, PatientUpdate, SensorModeChange, Unmeasured
from app.services.bp_service import BloodPressureEstimator
from app.services.handover_pdf import build_handover_pdf
from app.services.label_printer import LabelPrinter, PrinterError, sample_detail
from app.services.monitor import HANDOVER_REQUIRED, Monitor
from app.services.patients import (
    active_patient,
    create_patient,
    patient_detail,
    serialize_patient,
    update_patient,
)
from app.services.sensor_service import SensorManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

sensor_manager = SensorManager()
bp_estimator = BloodPressureEstimator()
monitor = Monitor(sensor_manager, bp_estimator)
label_printer = LabelPrinter()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_db_and_tables()
    if settings.RUN_BACKGROUND:
        sensor_manager.start()
        monitor.start()
    yield
    monitor.stop()
    sensor_manager.stop()


app = FastAPI(title="SIREN Ambulance Triage", version="2.0.0", lifespan=lifespan)
app.add_exception_handler(RequestValidationError, errors.validation_error)
app.add_exception_handler(StarletteHTTPException, errors.http_error)
app.add_exception_handler(Exception, errors.unexpected_error)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _page(request: Request, name: str, **context) -> HTMLResponse:
    """Render a page. The app bar reads page_title, back_href/back_label and,
    inside the Pantau Pasien flow, flow_step (1 data, 2 pemantauan, 3 serah terima)."""
    return templates.TemplateResponse(request, name, {
        "ambulance_id": settings.AMBULANCE_ID,
        "mock_mode": sensor_manager.mode == "MOCK",
        "page_title": "SIREN",
        "back_href": "/",
        "back_label": "Beranda",
        "flow_step": None,
        **context,
    })


def _get_patient(db: Session, patient_id: str) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Pasien tidak ditemukan.")
    return patient


def _require_active(patient: Patient) -> None:
    if patient.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Pasien sudah diserahterimakan; data tidak dapat diubah.")


# ------------------------------------------------------------------- pages --
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    current = active_patient(db)
    return _page(request, "home.html", nav="home", page_title="Beranda", back_href=None,
                 active=serialize_patient(current) if current else None)


@app.get("/pantau", include_in_schema=False)
def pantau(db: Session = Depends(get_db)):
    """The Pantau Pasien button: carry on with the patient in the ambulance, or start a new one."""
    return RedirectResponse("/monitor" if active_patient(db) else "/start", status_code=303)


# Old routes from the disaster-triage flow
@app.get("/landing", include_in_schema=False)
@app.get("/form", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
def legacy_routes():
    return RedirectResponse("/", status_code=303)


@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request, db: Session = Depends(get_db)):
    current = active_patient(db)
    if current:
        return RedirectResponse("/monitor", status_code=303)
    destinations = db.scalars(
        select(Patient.destination).where(Patient.destination.is_not(None)).distinct().limit(30)
    ).all()
    return _page(request, "start.html", destinations=destinations, nav="start",
                 page_title="Data pasien", flow_step=1)


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request, db: Session = Depends(get_db)):
    if not active_patient(db):
        return RedirectResponse("/start", status_code=303)
    return _page(request, "monitor.html", nav="monitor", page_title="Pemantauan", flow_step=2)


@app.get("/patients/{patient_id}", response_class=HTMLResponse)
def handover_page(request: Request, patient_id: str, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    if patient.status == "ACTIVE" and not _ready_for_handover():
        # Handover opens only once all required values are in or excused
        return RedirectResponse("/monitor?blocked=1", status_code=303)
    if patient.status == "ACTIVE":
        return _page(request, "handover.html", patient_id=patient.id, nav="monitor",
                     page_title="Serah terima", flow_step=3)
    # A finished patient opened from Riwayat: read-only summary, back to the list
    return _page(request, "handover.html", patient_id=patient.id, nav="history",
                 page_title=f"Ringkasan {patient.code}", back_href="/history", back_label="Riwayat")


def _ready_for_handover() -> bool:
    live = monitor.tick()
    return bool(live.get("readiness", {}).get("ready"))


def _not_ready_detail(live: dict) -> str:
    missing = [item["label"] for item in live["readiness"]["items"] if item["state"] == "missing"]
    return (f"Data belum lengkap: {', '.join(missing)}. Isi nilainya, atau tandai "
            "'tidak dapat diukur' dengan alasan, sebelum serah terima.")


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return _page(request, "history.html", nav="history", page_title="Riwayat pasien")


@app.get("/sensors", response_class=HTMLResponse)
def sensors_page(request: Request):
    return _page(request, "sensors.html", nav="sensors", page_title="Perangkat")


# --------------------------------------------------------------------- live --
@app.get("/api/live")
def live():
    return JSONResponse(monitor.latest or monitor.tick())


@app.get("/api/sensors/status")
def sensor_status():
    return sensor_manager.status_payload()


@app.post("/api/sensors/rescan")
def rescan_sensors():
    sensor_manager.rescan()
    return sensor_manager.status_payload()


def _mode_state(db: Session) -> dict:
    current = active_patient(db)
    return {
        "mode": sensor_manager.mode,
        "scenario": sensor_manager.scenario,
        "can_switch": current is None,
        "active_patient": current.code if current else None,
    }


@app.get("/api/sensors/mode")
def get_sensor_mode(db: Session = Depends(get_db)):
    return _mode_state(db)


@app.post("/api/sensors/mode")
def set_sensor_mode(body: SensorModeChange, db: Session = Depends(get_db)):
    current = active_patient(db)
    if current:
        # One patient, one data source from pickup to handover
        raise HTTPException(
            status_code=409,
            detail=f"Ada pasien aktif ({current.code}). Selesaikan serah terima dulu sebelum mengganti sumber data sensor.",
        )
    if body.mode != sensor_manager.mode or (body.scenario and body.scenario != sensor_manager.scenario):
        sensor_manager.set_mode(body.mode, body.scenario)
        monitor.tick()
    return _mode_state(db)


@app.get("/api/sensor-events")
def sensor_events(db: Session = Depends(get_db)):
    events = db.scalars(select(SensorEvent).order_by(desc(SensorEvent.created_at)).limit(20)).all()
    return {"items": [{
        "sensor": e.sensor, "status": e.status, "port": e.port, "details": e.details,
        "created_at": e.created_at.isoformat(),
    } for e in events]}


# ----------------------------------------------------------------- patients --
@app.post("/api/patients", status_code=201)
def new_patient(body: PatientCreate, db: Session = Depends(get_db)):
    current = active_patient(db)
    if current:
        raise HTTPException(
            status_code=409,
            detail=f"Masih ada pasien aktif ({current.code}). Selesaikan serah terima terlebih dahulu.",
        )
    patient = create_patient(db, body)
    monitor.tick()
    return {"patient": serialize_patient(patient)}


@app.get("/api/patients")
def list_patients(db: Session = Depends(get_db)):
    patients = db.scalars(select(Patient).order_by(desc(Patient.created_at)).limit(200)).all()
    items = []
    for patient in patients:
        latest = db.scalars(
            select(TriageResult).where(TriageResult.patient_id == patient.id).order_by(desc(TriageResult.created_at))
        ).first()
        items.append({
            **serialize_patient(patient),
            "latest_colour": latest.colour if latest else None,
            "latest_news2": latest.news2_total if latest else None,
        })
    return {"items": items}


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str, db: Session = Depends(get_db)):
    return patient_detail(db, _get_patient(db, patient_id))


@app.patch("/api/patients/{patient_id}")
def edit_patient(patient_id: str, body: PatientUpdate, undo: bool = False, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    _require_active(patient)
    changes = update_patient(db, patient, body, undo=undo)
    if undo:
        # The colour hold smooths sensor noise; it must not keep showing the
        # result of a key pressed by mistake.
        monitor.reset_hold()
    return {"changes": changes, "live": monitor.tick()}


@app.post("/api/patients/{patient_id}/manual-vitals")
def manual_vitals(patient_id: str, body: ManualVitals, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    _require_active(patient)
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="Isi minimal satu tanda vital.")
    labels = {"hr": "Nadi", "spo2": "SpO2", "rr": "Laju napas", "temp": "Suhu"}
    detail = "; ".join(
        f"{labels[k]} manual {'dihapus' if v is None else v}" for k, v in values.items()
    )
    db.add(PatientEvent(patient_id=patient.id, kind="MANUAL_VITAL", detail=detail, data=values))
    db.commit()
    monitor.set_manual(values)
    return {"live": monitor.tick()}


# ----------------------------------------------------------- notifications --
LEVEL_ORDER = {"alarm": 0, "beep": 1}


def _serialize_notification(event: PatientEvent, patient: Patient) -> dict:
    data = event.data or {}
    return {
        "id": event.id,
        "patient_id": patient.id,
        "patient_code": patient.code,
        "kind": data.get("kind"),
        "level": data.get("level"),
        "title": data.get("title"),
        "message": data.get("message"),
        "created_at": event.created_at.isoformat(),
    }


@app.get("/api/notifications")
def pending_notifications(db: Session = Depends(get_db)):
    """Deterioration alerts for the active patient that nobody has acknowledged yet."""
    patient = active_patient(db)
    if patient is None:
        return {"items": []}
    events = db.scalars(
        select(PatientEvent)
        .where(PatientEvent.patient_id == patient.id, PatientEvent.kind == "NOTIFIKASI")
        .order_by(PatientEvent.created_at)
    ).all()
    pending = [e for e in events if not (e.data or {}).get("ack_at")]
    # Alarms (MERAH) first, then the oldest unanswered
    pending.sort(key=lambda e: (LEVEL_ORDER.get((e.data or {}).get("level"), 9), e.created_at))
    return {"items": [_serialize_notification(e, patient) for e in pending]}


@app.post("/api/notifications/{event_id}/ack")
def acknowledge_notification(event_id: str, db: Session = Depends(get_db)):
    event = db.get(PatientEvent, event_id)
    if event is None or event.kind != "NOTIFIKASI":
        raise HTTPException(status_code=404, detail="Notifikasi tidak ditemukan.")
    data = dict(event.data or {})
    if not data.get("ack_at"):
        now = utcnow()
        data["ack_at"] = now.isoformat()
        event.data = data   # reassign so SQLAlchemy sees the JSON change
        event.detail = f"{event.detail} Dikonfirmasi petugas {now.astimezone():%H:%M:%S}."
        db.commit()
    return {"ok": True, "ack_at": data["ack_at"]}


@app.post("/api/patients/{patient_id}/unmeasured")
def mark_unmeasured(patient_id: str, body: Unmeasured, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    _require_active(patient)
    label = HANDOVER_REQUIRED[body.param]
    reason = body.reason.strip() if body.reason else None
    detail = f"{label} tidak dapat diukur: {reason}" if reason else f"{label}: tanda 'tidak dapat diukur' dicabut"
    db.add(PatientEvent(patient_id=patient.id, kind="TIDAK_DIUKUR", detail=detail,
                        data={"param": body.param, "reason": reason}))
    db.commit()
    monitor.set_unmeasured(body.param, reason)
    return {"live": monitor.tick()}


@app.post("/api/patients/{patient_id}/notes", status_code=201)
def add_note(patient_id: str, body: NoteCreate, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    _require_active(patient)
    db.add(PatientEvent(patient_id=patient.id, kind="TINDAKAN", detail=body.text.strip()))
    db.commit()
    return {"ok": True}


@app.post("/api/patients/{patient_id}/handover")
def handover(patient_id: str, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    _require_active(patient)
    live = monitor.tick(force_snapshot=True)  # one last snapshot while the patient is still active
    if not live["readiness"]["ready"]:
        raise HTTPException(status_code=409, detail=_not_ready_detail(live))
    patient.status = "HANDED_OVER"
    patient.handed_over_at = utcnow()
    db.add(PatientEvent(patient_id=patient.id, kind="STATUS", detail="Pasien diserahterimakan ke rumah sakit"))
    db.commit()
    monitor.tick()
    return {"patient": serialize_patient(patient)}


@app.get("/api/patients/{patient_id}/handover.pdf")
def handover_pdf(patient_id: str, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    detail = patient_detail(db, patient)
    # Simulated or not is a property of this patient's data, not of today's mode
    simulated = any("MOCK" in (r.get("sources") or {}).values() for r in detail["timeline"])
    pdf = build_handover_pdf(detail, patient.ambulance_id or settings.AMBULANCE_ID, bp_estimator.label(),
                             "MOCK" if simulated else "SERIAL")
    filename = f"serah-terima-{patient.code}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ------------------------------------------------------------------ printer --
@app.get("/api/printer/status")
def printer_status():
    return label_printer.status()


@app.post("/api/printer/test")
def printer_test():
    try:
        message = label_printer.print_label(sample_detail(settings.AMBULANCE_ID))
    except PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {"ok": True, "message": message}


@app.post("/api/patients/{patient_id}/print-label")
def print_label(patient_id: str, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    if patient.status == "ACTIVE":
        monitor.tick(force_snapshot=True)  # label carries the newest values, not ones up to 10 s old
    try:
        message = label_printer.print_label(patient_detail(db, patient))
    except PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    db.add(PatientEvent(patient_id=patient.id, kind="CETAK", detail="Label serah terima dicetak"))
    db.commit()
    return {"ok": True, "message": message}


@app.get("/api/patients/{patient_id}/label.png")
def label_preview(patient_id: str, db: Session = Depends(get_db)):
    patient = _get_patient(db, patient_id)
    return Response(label_printer.render_png(patient_detail(db, patient)), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------- export --
def _csv(filename: str, header: list[str], rows) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/patients.csv")
def export_patients(db: Session = Depends(get_db)):
    patients = db.scalars(select(Patient).order_by(Patient.created_at)).all()
    return _csv("siren-pasien.csv",
                ["code", "status", "name", "age", "sex", "complaint", "comorbid", "destination", "crew",
                 "acvpu", "on_oxygen", "pain", "systolic", "diastolic", "created_at", "handed_over_at"],
                [[p.code, p.status, p.name, p.age, p.sex, p.complaint, p.comorbid, p.destination, p.crew,
                  p.acvpu, p.on_oxygen, p.pain, p.systolic, p.diastolic,
                  p.created_at.isoformat() if p.created_at else "",
                  p.handed_over_at.isoformat() if p.handed_over_at else ""] for p in patients])


@app.get("/api/export/readings.csv")
def export_readings(db: Session = Depends(get_db)):
    rows = db.execute(
        select(VitalReading, TriageResult, Patient.code)
        .join(Patient, Patient.id == VitalReading.patient_id)
        .outerjoin(TriageResult, TriageResult.reading_id == VitalReading.id)
        .order_by(VitalReading.captured_at)
    ).all()
    return _csv("siren-pembacaan.csv",
                ["patient_code", "captured_at", "hr", "spo2", "rr", "temp", "pi", "motion_g", "sources",
                 "cuff_systolic", "cuff_diastolic", "bp_est_sbp", "bp_est_dbp", "bp_est_status",
                 "colour", "colour_source", "news2_total", "news2_measured", "start_colour", "delta", "shock_index"],
                [[code, r.captured_at.isoformat(), r.hr, r.spo2, r.rr, r.temp, r.pi, r.motion_g, r.sources,
                  r.systolic, r.diastolic, r.bp_est_sbp, r.bp_est_dbp, r.bp_est_status,
                  t.colour if t else None, t.source if t else None, t.news2_total if t else None,
                  t.news2_measured if t else None, t.start_colour if t else None,
                  t.delta if t else None, t.shock_index if t else None] for r, t, code in rows])
