"""Load the saved forest and predict SBP/DBP. No siren_app imports."""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import joblib
import numpy as np

from .features import extract_features
from .preprocess import DEFAULT_REFERENCE, prepare_window

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"


def news2_sbp_score(sbp: float) -> int:
    """NEWS2 systolic BP sub-score (RCP 2017)."""
    if sbp <= 90:
        return 3
    if sbp <= 100:
        return 2
    if sbp <= 110:
        return 1
    if sbp <= 219:
        return 0
    return 3


class BpModel:
    def __init__(self, model_dir: str | Path = MODEL_DIR):
        model_dir = Path(model_dir)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            bundle = joblib.load(model_dir / "bp_model.joblib")
        self.saved_sklearn_version = "same as installed"
        for w in caught:
            v = getattr(w.message, "original_sklearn_version", None)
            if v:
                self.saved_sklearn_version = v
                print(f"Note: model saved with scikit-learn {v}; install that version "
                      "(requirements.txt) for exact results.")
                break
        self.bundle = bundle
        self.model = bundle["model"]
        # Single thread: identical answers on every run (parallel trees can
        # differ in the last decimal place).
        self.model.n_jobs = 1
        self.feature_version = bundle.get("feature_version")
        self.metrics = None
        self.reference = dict(DEFAULT_REFERENCE)
        metrics_path = model_dir / "bp_model_metrics.json"
        if metrics_path.exists():
            self.metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            self.reference = self.metrics.get("ppg_reference", self.reference)

    def predict_window(self, window_125hz: np.ndarray) -> tuple[float, float]:
        """A ready 3750-sample window, as in the MIMIC dataset."""
        sbp, dbp = self.model.predict(extract_features(window_125hz).reshape(1, -1))[0]
        return float(sbp), float(dbp)

    def predict_many(self, windows: np.ndarray) -> np.ndarray:
        X = np.vstack([extract_features(w) for w in windows])
        return self.model.predict(X)

    def predict_from_sensor(self, times_ms: np.ndarray, raw_ir: np.ndarray):
        """Raw MAX30102 IR, exactly the path siren_app takes. Returns (sbp, dbp, status)."""
        window, status, _ = prepare_window(times_ms, raw_ir, self.reference)
        if window is None:
            return None, None, status
        sbp, dbp = self.predict_window(window)
        return sbp, dbp, "OK"
