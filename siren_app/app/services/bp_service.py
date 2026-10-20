"""Experimental blood-pressure estimate from the sensor's raw PPG waveform.

Display only. Triage uses the cuff value the crew types in (final report,
Tabel 3-5): this model's typical error is too large to move a NEWS2 band.

The model was trained on MIMIC-BP: 30 s windows of a bedside monitor's pleth
at 125 Hz, in that monitor's arbitrary units. The MAX30102 gives raw IR counts
at roughly 10-25 Hz, upside down (reflectance IR dips at systole). So before
inference the window is inverted, resampled to 125 Hz and rescaled to the
training set's typical level and spread. That makes the input the right shape
for the model; it does not make the model validated on this sensor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from app.config import settings

log = logging.getLogger("siren.bp")

FS_MODEL = 125.0
WINDOW_S = 30.0
WINDOW_SAMPLES = int(FS_MODEL * WINDOW_S)  # 3750
MIN_COVERAGE_S = 28.0
MAX_GAP_S = 0.5
# Used only when bp_model_metrics.json is missing. Same values it contains.
DEFAULT_REFERENCE = {"mean": 1.6717, "std": 0.4202}


@dataclass
class BpEstimate:
    status: str                 # OK | NO_MODEL | NO_WAVEFORM | COLLECTING | POOR_SIGNAL
    message: str
    sbp: float | None = None
    dbp: float | None = None
    coverage_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_features(signal: np.ndarray) -> np.ndarray:
    """Must stay identical to BloodPressure-Model/train_bp_model.py (feature_version signal_summary_v1)."""
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size == 0:
        return np.zeros(26, dtype=np.float64)

    centered = sig - np.mean(sig)
    diff = np.diff(sig)
    abs_diff = np.abs(diff)
    fft_vals = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(sig.size, d=1 / 125.0)

    feature_list = [
        np.mean(sig),
        np.std(sig),
        np.min(sig),
        np.max(sig),
        np.median(sig),
        np.ptp(sig),
        np.sqrt(np.mean(sig ** 2)),
        np.mean(np.abs(sig)),
        np.mean(abs_diff),
        np.max(abs_diff),
        np.std(diff),
        np.mean(np.abs(centered)),
        np.mean(np.abs(np.diff(sig, n=2))),
        np.sum(np.abs(sig)),
        np.sum(np.square(sig)),
        np.sum(sig > np.median(sig)),
        np.sum(sig < np.median(sig)),
        float(np.sum(centered > 0) / max(len(centered), 1)),
        float(np.sum(centered < 0) / max(len(centered), 1)),
        float(np.sum(np.diff(np.sign(centered)) != 0) / max(len(centered), 1)),
    ]

    spectral = []
    if fft_vals.size > 0:
        top_idx = np.argsort(fft_vals)[-6:]
        spectral.extend(fft_vals[top_idx].tolist())
        spectral.append(float(freqs[np.argmax(fft_vals)]))
        spectral.append(float(np.sum(fft_vals[1:10]) / max(np.sum(fft_vals), 1e-9)))
    else:
        spectral.extend([0.0, 0.0, 0.0])

    downsampled = np.asarray(np.linspace(sig.min(), sig.max(), num=8))
    feature_list.extend(downsampled.tolist())
    feature_list.extend(spectral)
    return np.asarray(feature_list, dtype=np.float64)


def prepare_window(times_ms: np.ndarray, values: np.ndarray, reference: dict[str, float]) -> tuple[np.ndarray | None, str, float]:
    """Turn the newest 30 s of raw IR into a model-ready 3750-sample window.

    Returns (window or None, reason code, seconds of signal available).
    """
    if times_ms.size < 2:
        return None, "NO_WAVEFORM", 0.0
    coverage = (times_ms[-1] - times_ms[0]) / 1000.0
    if coverage < MIN_COVERAGE_S:
        return None, "COLLECTING", coverage
    if np.max(np.diff(times_ms)) / 1000.0 > MAX_GAP_S:
        return None, "COLLECTING", coverage

    grid = np.linspace(times_ms[-1] - WINDOW_S * 1000.0, times_ms[-1], WINDOW_SAMPLES)
    resampled = np.interp(grid, times_ms, values)

    inverted = -resampled
    spread = np.std(inverted)
    if not np.isfinite(spread) or spread < 1e-6:
        return None, "POOR_SIGNAL", coverage
    scaled = (inverted - np.mean(inverted)) / spread * reference["std"] + reference["mean"]
    return scaled, "OK", coverage


class BloodPressureEstimator:
    def __init__(self, model_path: Path = settings.BP_MODEL_PATH, metrics_path: Path = settings.BP_METRICS_PATH):
        self.model_path = model_path
        self.metrics_path = metrics_path
        self.model = None
        self.feature_version: str | None = None
        self.metrics: dict[str, Any] | None = None
        self.reference = dict(DEFAULT_REFERENCE)
        self._load()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def _load(self) -> None:
        if not self.model_path.exists():
            log.warning("BP model not found at %s; estimates disabled", self.model_path)
            return
        try:
            bundle = joblib.load(self.model_path)
            self.model = bundle.get("model")
            self.feature_version = bundle.get("feature_version")
        except Exception:
            log.exception("Could not load BP model from %s", self.model_path)
            self.model = None
        if self.metrics_path.exists():
            try:
                self.metrics = json.loads(self.metrics_path.read_text(encoding="utf-8"))
                self.reference = self.metrics.get("ppg_reference", self.reference)
            except (OSError, json.JSONDecodeError):
                log.exception("Could not read BP metrics from %s", self.metrics_path)

    def label(self) -> dict[str, Any]:
        """The accuracy statement shown next to every estimate."""
        out: dict[str, Any] = {
            "experimental": True,
            "model_version": self.feature_version,
            "notes": [
                "Estimasi eksperimental, tidak dipakai untuk triase.",
                "Belum divalidasi pada sensor MAX30102 ini; akurasi nyata kemungkinan lebih buruk.",
                "Bukan pengganti manset.",
            ],
        }
        if self.metrics:
            sbp = self.metrics["targets"]["sbp"]
            dbp = self.metrics["targets"]["dbp"]
            out.update({
                "sbp_mae": sbp["MAE"],
                "dbp_mae": dbp["MAE"],
                "sbp_within_10_pct": sbp["within_mmHg_pct"]["10"],
                "dbp_within_10_pct": dbp["within_mmHg_pct"]["10"],
                "test_subjects": self.metrics.get("test_subjects"),
                "dataset": self.metrics.get("dataset"),
            })
        return out

    def estimate(self, times_ms: np.ndarray, values: np.ndarray, finger: bool | None, motion_g: float | None) -> BpEstimate:
        if not self.loaded:
            return BpEstimate("NO_MODEL", "Model BP tidak ditemukan di perangkat.")
        if times_ms.size == 0:
            return BpEstimate("NO_WAVEFORM", "Menunggu gelombang PPG dari sensor (firmware dengan WAVEFORM_OUTPUT).")
        if finger is False:
            return BpEstimate("POOR_SIGNAL", "Jari tidak terdeteksi pada sensor PPG.")
        if motion_g is not None and motion_g > settings.MOTION_FLAG_G:
            return BpEstimate("POOR_SIGNAL", "Getaran kendaraan terlalu tinggi untuk estimasi.")

        window, reason, coverage = prepare_window(times_ms, values, self.reference)
        if window is None:
            messages = {
                "COLLECTING": f"Mengumpulkan sinyal PPG ({coverage:.0f}/{WINDOW_S:.0f} detik).",
                "POOR_SIGNAL": "Sinyal PPG datar, periksa posisi jari.",
            }
            return BpEstimate(reason, messages.get(reason, "Gelombang PPG belum tersedia."), coverage_s=round(coverage, 1))

        try:
            pred = self.model.predict(extract_features(window).reshape(1, -1))[0]
        except Exception:
            log.exception("BP inference failed")
            return BpEstimate("NO_MODEL", "Model BP gagal dijalankan.")
        return BpEstimate("OK", "Estimasi dari 30 detik gelombang PPG.", round(float(pred[0]), 1), round(float(pred[1]), 1), round(coverage, 1))
