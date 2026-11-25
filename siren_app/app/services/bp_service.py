"""Blood pressure from the PPG waveform, calibrated with the cuff.

The model (app/bp_calibrated, trained in siren-bp-model-test on VitalDB) does
not guess an absolute pressure. Every cuff reading is paired with the 30 s of
PPG recorded just before the cuff squeezed the arm; afterwards the model
predicts how much SBP and DBP changed since that reading from how the pulse
shape changed, and fuses that with the cuff values in a Kalman filter. The
estimate is therefore only as fresh as the last cuff, and the model says when
the next cuff is due (every 10 min, 5 min when NEWS2 >= 5, earlier when the
estimate moves 5 mmHg, changes NEWS2 band, drops, is low, or the pulse rate
changes by a quarter).

While the estimate is usable (status OK, or AWAITING_CUFF while a requested
cuff is pending) the monitor scores NEWS2 systolic from it, taking the
cautious value the model was validated with: estimate - 0.3 sigma.

This module keeps the crew-facing wording; the model's internals (Kalman
sigma, gain, anchors) stay out of the UI.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger("siren.bp")

FINGER_LOST_RESET_S = 5.0      # finger off this long: the stored pulse shapes no longer apply
CUFF_START_KEEP_S = 600.0      # a "Mulai ukur" window older than this is not used
GAIN_HOLD_S = 60.0             # keep showing the last estimate this long across an LED gain step
USABLE = ("OK", "AWAITING_CUFF")

# Why the model asks for a cuff, in the crew's words
REASONS = {
    "INTERVAL": "jadwal rutin",
    "SMART_CHANGE": "estimasi TD berubah sejak manset terakhir",
    "NEWS2_BAND_CHANGE": "estimasi TD pindah ke rentang skor NEWS2 lain",
    "DROP_ALERT": "estimasi TD turun",
    "HYPOTENSION_ALERT": "estimasi TD rendah",
    "RISE_ALERT": "estimasi TD naik tajam",
    "HR_CHANGE": "nadi berubah banyak sejak manset terakhir",
    "CONFIRM": "memastikan pengukuran sebelumnya",
    "UNCERTAINTY": "estimasi makin tidak pasti",
}
# Why a cuff reading could not calibrate the estimate
CALIBRATION_FAILED = {
    "MOTION": "kendaraan atau pasien sedang bergerak",
    "POOR_SIGNAL": "sinyal PPG kurang stabil",
    "NO_BEATS": "denyut tidak terbaca di sensor jari",
    "NOT_ENOUGH_SIGNAL": "sinyal PPG belum cukup 30 detik",
    "COLLECTING": "sinyal PPG belum cukup 30 detik",
    "GAIN_CHANGE": "sensor sedang menyesuaikan cahaya",
    "NO_WAVEFORM": "sensor PPG belum mengirim gelombang",
    "NO_MODEL": "model TD tidak tersedia di perangkat",
}
STATUS_MESSAGE = {
    "NO_MODEL": "Model TD tidak ditemukan di perangkat.",
    "NO_WAVEFORM": "Menunggu gelombang dari sensor jari.",
    "COLLECTING": "Mengumpulkan sinyal 30 detik.",
    "POOR_SIGNAL": "Sinyal kurang stabil, tahan jari tetap diam.",
    "NEEDS_CALIBRATION": "Ukur TD manset sekali untuk memulai estimasi.",
    "FINGER_RESET": "Sensor jari sempat lepas. Ukur TD manset lagi untuk melanjutkan.",
    "STALE": "Manset terakhir lebih dari 20 menit lalu. Ukur TD manset.",
}


@dataclass
class BpEstimate:
    status: str                    # OK | AWAITING_CUFF | STALE | NEEDS_CALIBRATION | COLLECTING | POOR_SIGNAL | NO_WAVEFORM | NO_MODEL
    message: str
    sbp: float | None = None
    dbp: float | None = None
    news2_sbp: float | None = None  # the value NEWS2 systolic is scored from (estimate - z sigma)
    usable: bool = False            # may stand in for the cuff in triage
    cuff_now: bool = False
    reasons: list[str] = field(default_factory=list)
    next_cuff_min: float | None = None
    minutes_since_cuff: float | None = None
    calibrated_at: float | None = None   # wall time of the last cuff that calibrated
    range_95: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["reason_text"] = [REASONS[r] for r in self.reasons if r in REASONS]
        return out

    @property
    def reminder(self) -> str:
        """Why the crew should take a cuff now, for the reminder banner."""
        words = [REASONS[r] for r in self.reasons if r in REASONS]
        if not words:
            return "Jadwal rutin."
        text = ", ".join(words)
        return text[0].upper() + text[1:] + "."


@dataclass
class Calibration:
    ok: bool
    reason: str
    message: str


class BloodPressureEstimator:
    def __init__(self, model_path: Path = settings.BP_MODEL_PATH, settings_path: Path = settings.BP_SETTINGS_PATH,
                 entry_lag_s: float = settings.BP_CUFF_ENTRY_LAG_S):
        self.model_path = Path(model_path)
        self.settings_path = Path(settings_path)
        self.entry_lag_s = entry_lag_s
        self.model = None
        self._lock = threading.Lock()
        self._load()
        self._clear()

    def _load(self) -> None:
        if not self.model_path.exists():
            log.warning("BP model not found at %s; estimates disabled", self.model_path)
            return
        try:
            from app.bp_calibrated.calibrated import CalibratedBpModel
            self.model = CalibratedBpModel(self.model_path, self.settings_path)
        except Exception:
            log.exception("Could not load BP model from %s", self.model_path)
            self.model = None

    def _clear(self) -> None:
        self.calibrated_at: float | None = None
        self.cuff_start: tuple[float, Any, Any, float | None] | None = None   # (wall time, times, values, motion)
        self.finger_off_since: float | None = None
        self.finger_reset = False
        self.wave_resets_seen: int | None = None
        self.last: tuple[float, BpEstimate] | None = None

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def label(self) -> dict[str, Any]:
        """What the model is, for the handover PDF and the help text."""
        out: dict[str, Any] = {
            "experimental": False,
            "calibrated": True,
            "model_version": getattr(self.model, "name", None),
            "notes": [
                "Estimasi dari gelombang PPG, dikalibrasi dengan TD manset dan dipakai untuk skor NEWS2 sistolik.",
                "Manset diminta berkala (tiap 10 menit, 5 menit bila NEWS2 5 atau lebih) dan saat estimasi berubah.",
                "Data latih: VitalDB (pasien operasi). Belum divalidasi pada relawan SIREN.",
            ],
        }
        metrics = getattr(self.model, "metrics", None) or {}
        if metrics.get("SBP"):
            out.update({
                "sbp_mae": round(metrics["SBP"]["MAE"], 1),
                "dbp_mae": round(metrics["DBP"]["MAE"], 1),
                "sbp_within_10_pct": round(metrics["SBP"]["within10"], 1),
                "dataset": "VitalDB",
            })
        return out

    # ---------------------------------------------------------------- cuff --
    def reset(self) -> None:
        """New patient, or a cuff entry undone: start again from the next cuff."""
        with self._lock:
            if self.model is not None:
                self.model.reset()
            self._clear()

    def mark_cuff_start(self, sensors) -> Calibration:
        """The crew starts the cuff: keep the PPG window from before inflation."""
        times, values, why = sensors.bp_window(0.0)
        if times is None:
            with self._lock:
                self.cuff_start = None
            return Calibration(False, why, CALIBRATION_FAILED.get(why, "sinyal PPG belum siap"))
        with self._lock:
            self.cuff_start = (time.time(), times, values, sensors.motion_median(30.0))
        return Calibration(True, "OK", "Sinyal PPG sebelum manset tersimpan.")

    def calibrate(self, sensors, sbp: float, dbp: float | None) -> Calibration:
        """Every cuff reading. The cuff value counts for triage either way."""
        if self.model is None:
            return Calibration(False, "NO_MODEL", CALIBRATION_FAILED["NO_MODEL"])
        if dbp is None:
            return Calibration(False, "NO_DBP", "isi juga diastolik agar estimasi TD bisa dikalibrasi")
        with self._lock:
            saved = self.cuff_start
            self.cuff_start = None
        if saved is not None and time.time() - saved[0] <= CUFF_START_KEEP_S:
            _, times, values, motion = saved
        else:
            times, values, why = sensors.bp_window(self.entry_lag_s)
            if times is None:
                return Calibration(False, why, CALIBRATION_FAILED.get(why, "sinyal PPG belum siap"))
            motion = sensors.motion_median(30.0, lag_s=self.entry_lag_s)
            if motion is None:      # no accelerometer values that far back: use the latest
                motion = sensors.motion_median(30.0)
        with self._lock:
            ok, why = self.model.calibrate(times, values, float(sbp), float(dbp), motion_g=motion)
            if ok:
                self.calibrated_at = time.time()
                self.finger_reset = False
                self.last = None
        if ok:
            return Calibration(True, "OK", "Estimasi TD dikalibrasi dengan manset ini.")
        return Calibration(False, why, CALIBRATION_FAILED.get(why, "sinyal PPG belum siap"))

    # ------------------------------------------------------------ estimate --
    def estimate(self, sensors, finger: bool | None, news2_total: int | None) -> BpEstimate:
        if self.model is None:
            return BpEstimate("NO_MODEL", STATUS_MESSAGE["NO_MODEL"])
        now = time.time()
        with self._lock:
            resets = sensors.wave_resets
            if self.wave_resets_seen is not None and resets != self.wave_resets_seen and self.model.cuffs:
                # The board restarted or lines were lost: device time and pulse baseline are gone
                self.model.reset()
                self.calibrated_at = None
                self.finger_reset = True
            self.wave_resets_seen = resets
            if finger is False:
                self.finger_off_since = self.finger_off_since or now
                if now - self.finger_off_since >= FINGER_LOST_RESET_S and self.model.cuffs:
                    self.model.reset()
                    self.calibrated_at = None
                    self.finger_reset = True
            else:
                self.finger_off_since = None

        times, values, why = sensors.bp_window(0.0)
        if times is None:
            if why == "GAIN_CHANGE" and self.last and now - self.last[0] <= GAIN_HOLD_S:
                return self.last[1]
            return BpEstimate(why, STATUS_MESSAGE.get(why, STATUS_MESSAGE["COLLECTING"]))
        with self._lock:
            r = self.model.estimate(times, values, news2_total=news2_total)
            z = float(getattr(self.model, "score_z", 0.0))
        status = r["status"]
        if status == "NEEDS_CALIBRATION":
            key = "FINGER_RESET" if self.finger_reset else "NEEDS_CALIBRATION"
            return BpEstimate(status, STATUS_MESSAGE[key], cuff_now=True)
        if status in ("COLLECTING", "POOR_SIGNAL"):
            return BpEstimate(status, STATUS_MESSAGE[status])

        sigma = r.get("sbp_sigma") or 0.0
        est = BpEstimate(
            status=status,
            message=STATUS_MESSAGE["STALE"] if status == "STALE" else "",
            sbp=r["sbp"], dbp=r["dbp"],
            news2_sbp=round(r["sbp"] - z * sigma, 1),
            usable=status in USABLE,
            cuff_now=bool(r["cuff_now"]) or status == "STALE",
            reasons=[x for x in r["recalibrate_reasons"] if x != "CUFF_OVERDUE"] if r["cuff_now"] else [],
            next_cuff_min=r["next_cuff_due_min"],
            minutes_since_cuff=r["minutes_since_cuff"],
            calibrated_at=self.calibrated_at,
            range_95=r.get("sbp_range_95"),
        )
        if "CUFF_OVERDUE" in r["recalibrate_reasons"]:
            est.reasons.append("CUFF_OVERDUE")
        self.last = (now, est)
        return est
