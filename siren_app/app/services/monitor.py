"""Continuous triage for the patient in the ambulance.

Once a second the monitor merges sensor and manual vitals, runs the two-track
triage (START + NEWS2, worse wins, delta-NEWS2 upgrade, see triage_policy.py
for the two agreed adjustments) and keeps the result for the screen. A
snapshot goes to the database every SNAPSHOT_INTERVAL_S and immediately
whenever the displayed colour changes, so the handover timeline is complete
even if power is lost.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.config import Settings, settings
from app.database import new_session, utcnow
from app.models import Patient, PatientEvent, TriageResult, VitalReading
from app.services import triage as TR
from app.services import triage_policy as policy
from app.services.bp_service import WINDOW_S, BloodPressureEstimator, BpEstimate
from app.services.sensor_service import SensorManager

log = logging.getLogger("siren.monitor")

COLOUR_CODE = {TR.HIJAU: "HIJAU", TR.KUNING: "KUNING", TR.MERAH: "MERAH"}
CODE_COLOUR = {code: value for value, code in COLOUR_CODE.items()}
VITAL_PARAMS = ("hr", "spo2", "rr", "temp")
MOTION_SENSITIVE = ("hr", "rr")
# Must have a value, or a written reason why it could not be measured, before
# handover. Consciousness and supplemental oxygen always have a value.
HANDOVER_REQUIRED = {
    "hr": "Nadi",
    "spo2": "SpO₂",
    "rr": "Laju napas",
    "temp": "Suhu",
    "systolic": "TD sistolik manset",
}
HISTORY_KEEP_S = 180
BP_REFRESH_S = 5.0


def colour_code(value: int | None) -> str | None:
    return None if value is None else COLOUR_CODE[value]


def age_flags(age: int | None, cfg: Settings = settings) -> tuple[bool, bool]:
    if age is None:
        return False, False
    return age >= cfg.ELDERLY_AGE, age < cfg.PAEDIATRIC_AGE


def operator_for(patient: Patient, cfg: Settings = settings) -> TR.Operator:
    elderly, paediatric = age_flags(patient.age, cfg)
    return TR.Operator(
        acvpu=patient.acvpu or "alert",
        on_oxygen=bool(patient.on_oxygen),
        uncontrolled_bleeding=bool(patient.uncontrolled_bleeding),
        limb_loss_risk=bool(patient.limb_loss_risk),
        ambulatory=bool(patient.ambulatory),
        pain=patient.pain,
        chest_pain=bool(patient.chest_pain),
        elderly=elderly,
        paediatric=paediatric,
        comorbid=bool(patient.comorbid),
        systolic=patient.systolic,
        diastolic=patient.diastolic,
    )


class Monitor:
    def __init__(self, sensors: SensorManager, bp: BloodPressureEstimator, cfg: Settings = settings):
        self.sensors = sensors
        self.bp = bp
        self.cfg = cfg
        self.patient_id: str | None = None
        # (time, NEWS2 parts) per evaluation, for the comparable delta
        self.history: deque[tuple[float, dict]] = deque(maxlen=HISTORY_KEEP_S * 2)
        self.hold = policy.ColourHold()
        self.clock = time.time   # tests replace this to move time forward
        self.manual: dict[str, tuple[float, float]] = {}   # param -> (value, epoch seconds)
        self.unmeasured: dict[str, dict[str, str]] = {}    # param -> {"reason", "at"}
        self.latest: dict[str, Any] | None = None
        self._last_snapshot = 0.0
        self._last_colour: str | None = None
        self._bp_cache: tuple[float, BpEstimate] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Monitor tick failed")
            self._stop.wait(1.0)

    # --------------------------------------------------------------- inputs --
    def set_manual(self, values: dict[str, float | None]) -> None:
        now = time.time()
        with self._lock:
            for param, value in values.items():
                if value is None:
                    self.manual.pop(param, None)
                else:
                    self.manual[param] = (float(value), now)

    def reset_hold(self) -> None:
        with self._lock:
            self.hold.reset()

    def set_unmeasured(self, param: str, reason: str | None) -> None:
        with self._lock:
            if reason is None:
                self.unmeasured.pop(param, None)
            else:
                self.unmeasured[param] = {"reason": reason, "at": utcnow().isoformat()}

    def readiness(self, vitals: dict[str, Any], patient: Patient) -> dict[str, Any]:
        """Whether handover may start: every required value present, or excused in writing."""
        items = []
        for param, label in HANDOVER_REQUIRED.items():
            value = patient.systolic if param == "systolic" else vitals[param]["value"]
            excused = self.unmeasured.get(param)
            if value is not None:
                state = "ok"
            elif excused:
                state = "excused"
            else:
                state = "missing"
            items.append({"param": param, "label": label, "state": state,
                          "reason": excused["reason"] if excused else None})
        missing = [item for item in items if item["state"] == "missing"]
        return {"ready": not missing, "missing": len(missing), "items": items}

    def manual_values(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        with self._lock:
            return {
                param: {"value": value, "age_s": round(now - at)}
                for param, (value, at) in self.manual.items()
                if now - at <= self.cfg.MANUAL_TTL_S
            }

    # ---------------------------------------------------------------- cycle --
    def tick(self, force_snapshot: bool = False) -> dict[str, Any]:
        """One evaluation. Called by the background thread and after every input.

        force_snapshot writes to the database now, for the final state before
        a handover or a printed label.
        """
        with self._lock:
            self.sensors.record_status_changes()
            db = new_session()
            try:
                patient = db.scalars(select(Patient).where(Patient.status == "ACTIVE").order_by(Patient.created_at.desc())).first()
                if patient is None:
                    self.patient_id = None
                    self.latest = self._payload(None, self._vitals(), None, None, None)
                    return self.latest
                if patient.id != self.patient_id:
                    self._activate(db, patient)

                vitals = self._vitals()
                op = operator_for(patient, self.cfg)
                tr_vitals = {p: vitals[p]["value"] for p in VITAL_PARAMS}
                tr_vitals["pi"] = vitals["_pi"]

                now = self.clock()
                if self.history and self.history[-1][0] > now:
                    self.history.clear()   # clock went backwards; see ColourHold.update
                result = policy.classify(tr_vitals, op, self._parts_a_window_ago(now))
                self.history.append((now, dict(result.news.parts)))
                while self.history and now - self.history[0][0] > HISTORY_KEEP_S:
                    self.history.popleft()
                shown = self.hold.update(now, result.colour)

                bp = self._bp_estimate(vitals)
                colour = COLOUR_CODE[shown.colour]
                changed = self._last_colour is not None and colour != self._last_colour
                due = now - self._last_snapshot >= self.cfg.SNAPSHOT_INTERVAL_S
                if self._last_colour is None or changed or due or force_snapshot:
                    self._persist(db, patient, vitals, result, shown, bp, previous=self._last_colour if changed else None)
                    self._last_snapshot = now
                self._last_colour = colour

                self.latest = self._payload(patient, vitals, result, shown, bp)
                return self.latest
            finally:
                db.close()

    def _parts_a_window_ago(self, now: float) -> dict | None:
        """NEWS2 parts from the newest evaluation at least DELTA_WINDOW_S old."""
        past = None
        for t, parts in self.history:
            if now - t >= TR.DELTA_WINDOW_S:
                past = parts
            else:
                break
        return past

    def _activate(self, db, patient: Patient) -> None:
        """Pick up a patient, including one that was active before a restart."""
        self.patient_id = patient.id
        self.history.clear()
        self.hold.reset()
        self.manual.clear()
        self.unmeasured.clear()
        self._last_colour = None
        self._last_snapshot = 0.0
        self._bp_cache = None

        cutoff = utcnow() - timedelta(seconds=self.cfg.MANUAL_TTL_S)
        events = db.scalars(
            select(PatientEvent)
            .where(PatientEvent.patient_id == patient.id, PatientEvent.kind == "MANUAL_VITAL", PatientEvent.created_at >= cutoff)
            .order_by(PatientEvent.created_at)
        ).all()
        for event in events:
            for param, value in (event.data or {}).items():
                if value is None:
                    self.manual.pop(param, None)
                else:
                    self.manual[param] = (float(value), event.created_at.timestamp())

        # Written reasons for values that could not be measured, newest wins
        excuses = db.scalars(
            select(PatientEvent)
            .where(PatientEvent.patient_id == patient.id, PatientEvent.kind == "TIDAK_DIUKUR")
            .order_by(PatientEvent.created_at)
        ).all()
        for event in excuses:
            data = event.data or {}
            if data.get("reason"):
                self.unmeasured[data["param"]] = {"reason": data["reason"], "at": event.created_at.isoformat()}
            else:
                self.unmeasured.pop(data.get("param"), None)

        recent = db.scalars(
            select(TriageResult)
            .where(TriageResult.patient_id == patient.id, TriageResult.created_at >= utcnow() - timedelta(seconds=HISTORY_KEEP_S))
            .order_by(TriageResult.created_at)
        ).all()
        for row in recent:
            t = row.created_at.timestamp()
            if row.news2_parts:
                self.history.append((t, row.news2_parts))
            self.hold.update(t, CODE_COLOUR.get(row.raw_colour or row.colour, TR.HIJAU))
        if recent:
            self._last_colour = recent[-1].colour

    def _vitals(self) -> dict[str, Any]:
        """Each vital with its source. Sensor first, then a recent manual entry."""
        readings = self.sensors.readings()
        sensor_source = "MOCK" if self.sensors.mode == "MOCK" else "SENSOR"
        motion = readings["motion_g"]
        shaking = motion is not None and motion > self.cfg.MOTION_FLAG_G
        manual = self.manual_values()

        out: dict[str, Any] = {}
        for param in VITAL_PARAMS:
            entry: dict[str, Any] = {"value": None, "source": None, "flags": []}
            if readings[param] is not None:
                entry.update(value=readings[param], source=sensor_source)
                if shaking and param in MOTION_SENSITIVE:
                    entry["flags"].append("getaran tinggi")
                if param == "temp" and readings["temp_via"]:
                    entry["via"] = readings["temp_via"]
            elif param in manual:
                entry.update(value=manual[param]["value"], source="MANUAL", age_s=manual[param]["age_s"])
            out[param] = entry
        out["_pi"] = readings["pi"]
        out["_motion_g"] = motion
        out["_finger"] = readings["finger"]
        return out

    def _bp_estimate(self, vitals: dict[str, Any]) -> BpEstimate:
        now = time.time()
        if self._bp_cache and now - self._bp_cache[0] < BP_REFRESH_S:
            return self._bp_cache[1]
        times, values, _fs = self.sensors.waveform(WINDOW_S + 2)
        estimate = self.bp.estimate(times, values, vitals["_finger"], vitals["_motion_g"])
        self._bp_cache = (now, estimate)
        return estimate

    def _persist(self, db, patient: Patient, vitals, result: TR.Triage, shown: policy.Displayed,
                 bp: BpEstimate, previous: str | None) -> None:
        reading = VitalReading(
            patient_id=patient.id,
            hr=vitals["hr"]["value"],
            spo2=vitals["spo2"]["value"],
            rr=vitals["rr"]["value"],
            temp=vitals["temp"]["value"],
            pi=vitals["_pi"],
            motion_g=vitals["_motion_g"],
            sources={p: vitals[p]["source"] for p in VITAL_PARAMS},
            systolic=patient.systolic,
            diastolic=patient.diastolic,
            bp_est_sbp=bp.sbp,
            bp_est_dbp=bp.dbp,
            bp_est_status=bp.status,
            quality={p: vitals[p]["flags"] for p in VITAL_PARAMS if vitals[p]["flags"]} or None,
        )
        db.add(reading)
        db.flush()
        colour = COLOUR_CODE[shown.colour]
        source = result.source if shown.hold_remaining_s is None else f"ditahan {COLOUR_CODE[shown.colour]}"
        db.add(TriageResult(
            patient_id=patient.id,
            reading_id=reading.id,
            colour=colour,
            raw_colour=COLOUR_CODE[shown.raw],
            source=source,
            news2_total=result.news.total,
            news2_measured=result.news.measured,
            news2_colour=colour_code(result.news.colour),
            news2_parts=result.news.parts,
            start_colour=colour_code(result.start.colour),
            start_reason=result.start.reason,
            delta=result.delta,
            shock_index=round(result.shock, 2) if result.shock is not None else None,
            alerts=result.alerts,
        ))
        if previous:
            stepped_down = CODE_COLOUR[previous] > shown.colour
            reason = f"stabil {policy.HOLD_S:.0f} detik" if stepped_down else result.source
            db.add(PatientEvent(
                patient_id=patient.id,
                kind="KATEGORI",
                detail=f"Kategori berubah {previous} → {colour} (sumber: {reason})",
                data={"from": previous, "to": colour, "source": reason},
            ))
        db.commit()

    # -------------------------------------------------------------- output --
    def _payload(self, patient: Patient | None, vitals, result: TR.Triage | None,
                 shown: policy.Displayed | None, bp: BpEstimate | None) -> dict[str, Any]:
        public_vitals = {p: vitals[p] for p in VITAL_PARAMS}
        payload: dict[str, Any] = {
            "updated_at": utcnow().isoformat(),
            "mode": self.sensors.mode,
            "ambulance_id": self.cfg.AMBULANCE_ID,
            "sensors": self.sensors.status_payload(),
            "vitals": public_vitals,
            "perfusion_index": vitals["_pi"],
            "motion_g": vitals["_motion_g"],
            "finger": vitals["_finger"],
            "patient": None,
            "triage": None,
            "bp_estimate": None,
        }
        if patient is None:
            return payload

        from app.services.patients import serialize_patient  # avoid an import cycle

        payload["patient"] = serialize_patient(patient)
        payload["cuff"] = {
            "systolic": patient.systolic,
            "diastolic": patient.diastolic,
            "at": patient.bp_cuff_at.isoformat() if patient.bp_cuff_at else None,
        }
        payload["bp_estimate"] = {**bp.to_dict(), "label": self.bp.label()}
        # Every manual value still in force, including ones a sensor overrides now
        payload["manual"] = self.manual_values()
        payload["readiness"] = self.readiness(vitals, patient)
        payload["triage"] = {
            "colour": COLOUR_CODE[shown.colour],
            "raw_colour": COLOUR_CODE[shown.raw],
            # Seconds until a lower colour may show; None when not holding
            "hold_remaining_s": None if shown.hold_remaining_s is None else round(shown.hold_remaining_s),
            "source": result.source,
            "news2": {
                "total": result.news.total,
                "measured": result.news.measured,
                "parts": result.news.parts,
                "colour": colour_code(result.news.colour),
                "note": result.news.note,
            },
            "start": {
                "colour": colour_code(result.start.colour),
                "reason": result.start.reason,
                "complete": result.start.complete,
            },
            "delta": result.delta,
            "shock_index": round(result.shock, 2) if result.shock is not None else None,
            "alerts": result.alerts,
        }
        return payload
