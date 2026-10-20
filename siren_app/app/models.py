from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime, utcnow

ACVPU_LEVELS = ("alert", "confusion", "voice", "pain", "unresponsive")


def _uuid() -> str:
    return str(uuid4())


class Patient(Base):
    """One patient, one transport. Ends at handover."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE | HANDED_OVER
    ambulance_id: Mapped[str | None] = mapped_column(String(40))
    crew: Mapped[str | None] = mapped_column(String(120))
    destination: Mapped[str | None] = mapped_column(String(160))

    # Identity is optional: an unresponsive patient cannot give a name.
    name: Mapped[str | None] = mapped_column(String(150))
    age: Mapped[int | None] = mapped_column(Integer)
    sex: Mapped[str | None] = mapped_column(String(20))
    complaint: Mapped[str | None] = mapped_column(Text)  # keluhan atau mekanisme cedera
    comorbid: Mapped[bool] = mapped_column(Boolean, default=False)

    # START criteria observed at pickup
    ambulatory: Mapped[bool] = mapped_column(Boolean, default=False)
    uncontrolled_bleeding: Mapped[bool] = mapped_column(Boolean, default=False)
    limb_loss_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    chest_pain: Mapped[bool] = mapped_column(Boolean, default=False)

    # Paramedic inputs that change during the journey
    acvpu: Mapped[str] = mapped_column(String(20), default="alert")
    on_oxygen: Mapped[bool] = mapped_column(Boolean, default=False)
    pain: Mapped[int | None] = mapped_column(Integer)
    systolic: Mapped[float | None] = mapped_column(Float)   # cuff, mmHg
    diastolic: Mapped[float | None] = mapped_column(Float)
    bp_cuff_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    handed_over_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    readings: Mapped[list["VitalReading"]] = relationship(back_populates="patient", order_by="VitalReading.captured_at")
    triage_results: Mapped[list["TriageResult"]] = relationship(back_populates="patient", order_by="TriageResult.created_at")
    events: Mapped[list["PatientEvent"]] = relationship(back_populates="patient", order_by="PatientEvent.created_at")


class VitalReading(Base):
    """A snapshot of every vital in use at one moment, with where each came from."""

    __tablename__ = "vital_readings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    patient_id: Mapped[str] = mapped_column(String(64), ForeignKey("patients.id"), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    hr: Mapped[float | None] = mapped_column(Float)
    spo2: Mapped[float | None] = mapped_column(Float)
    rr: Mapped[float | None] = mapped_column(Float)
    temp: Mapped[float | None] = mapped_column(Float)
    pi: Mapped[float | None] = mapped_column(Float)
    motion_g: Mapped[float | None] = mapped_column(Float)
    # {"hr": "SENSOR" | "MANUAL" | "MOCK" | None, ...}
    sources: Mapped[dict | None] = mapped_column(JSON)
    systolic: Mapped[float | None] = mapped_column(Float)   # cuff, copied at capture time
    diastolic: Mapped[float | None] = mapped_column(Float)
    bp_est_sbp: Mapped[float | None] = mapped_column(Float)  # model, display only
    bp_est_dbp: Mapped[float | None] = mapped_column(Float)
    bp_est_status: Mapped[str | None] = mapped_column(String(30))
    quality: Mapped[dict | None] = mapped_column(JSON)

    patient: Mapped[Patient] = relationship(back_populates="readings")


class TriageResult(Base):
    __tablename__ = "triage_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    patient_id: Mapped[str] = mapped_column(String(64), ForeignKey("patients.id"), nullable=False, index=True)
    reading_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("vital_readings.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    colour: Mapped[str] = mapped_column(String(10), nullable=False)  # HIJAU | KUNING | MERAH, as displayed
    raw_colour: Mapped[str | None] = mapped_column(String(10))       # this moment's evaluation, before the hold
    source: Mapped[str | None] = mapped_column(String(40))
    news2_total: Mapped[int | None] = mapped_column(Integer)
    news2_measured: Mapped[int] = mapped_column(Integer, default=0)
    news2_colour: Mapped[str | None] = mapped_column(String(10))
    news2_parts: Mapped[dict | None] = mapped_column(JSON)
    start_colour: Mapped[str | None] = mapped_column(String(10))
    start_reason: Mapped[str | None] = mapped_column(String(160))
    delta: Mapped[int | None] = mapped_column(Integer)
    shock_index: Mapped[float | None] = mapped_column(Float)
    alerts: Mapped[list | None] = mapped_column(JSON)

    patient: Mapped[Patient] = relationship(back_populates="triage_results")


class PatientEvent(Base):
    """Everything the crew entered or the system flagged, for the handover timeline."""

    __tablename__ = "patient_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    patient_id: Mapped[str] = mapped_column(String(64), ForeignKey("patients.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # START | INPUT | CUFF_BP | MANUAL_VITAL | TINDAKAN | KATEGORI | STATUS
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON)

    patient: Mapped[Patient] = relationship(back_populates="events")


class SensorEvent(Base):
    __tablename__ = "sensor_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    sensor: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    port: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
