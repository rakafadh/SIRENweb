import importlib.util
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services.bp_service import BloodPressureEstimator, extract_features, prepare_window
from app.services.sensor_service import MockFeeder, SensorManager

REFERENCE = {"mean": 1.67, "std": 0.42}
TRAIN_SCRIPT = Path(__file__).resolve().parents[2] / "BloodPressure-Model" / "train_bp_model.py"


def _wave(seconds: float, fs: float = 25.0):
    times = np.arange(0, seconds * 1000, 1000 / fs)
    values = 120000 - 900 * np.sin(2 * np.pi * 1.2 * times / 1000)
    return times, values


def test_window_is_resampled_inverted_and_rescaled():
    times, values = _wave(31)
    window, reason, coverage = prepare_window(times, values, REFERENCE)
    assert reason == "OK"
    assert window.shape == (3750,)
    assert coverage > 30
    assert window.mean() == pytest.approx(REFERENCE["mean"], abs=1e-6)
    assert window.std() == pytest.approx(REFERENCE["std"], abs=1e-6)
    # inverted: the reflectance dip at systole becomes a peak
    grid = np.linspace(times[-1] - 30000, times[-1], 3750)
    assert np.corrcoef(window, np.interp(grid, times, values))[0, 1] < -0.99


def test_short_or_broken_signal_is_rejected():
    times, values = _wave(10)
    assert prepare_window(times, values, REFERENCE)[1] == "COLLECTING"
    times, values = _wave(31)
    gap = np.concatenate([times[:200], times[230:]])
    assert prepare_window(gap, np.concatenate([values[:200], values[230:]]), REFERENCE)[1] == "COLLECTING"
    flat = np.full_like(times, 120000.0)
    assert prepare_window(times, flat, REFERENCE)[1] == "POOR_SIGNAL"


@pytest.mark.skipif(not TRAIN_SCRIPT.exists(), reason="training script not in this checkout")
def test_features_match_training_script():
    spec = importlib.util.spec_from_file_location("train_bp_model", TRAIN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    signal = np.random.default_rng(0).normal(1.6, 0.4, 3750)
    np.testing.assert_allclose(extract_features(signal), module.extract_features(signal))


@pytest.mark.skipif(not settings.BP_MODEL_PATH.exists(), reason="model artifact not present")
def test_estimate_from_mock_sensor_stream():
    manager = SensorManager()
    feeder = MockFeeder(manager)
    for second in range(32):
        for port, line in feeder.lines(second):
            manager.handle_line(port, line)
    times, values, fs = manager.waveform(32)
    assert fs == 25.0

    estimator = BloodPressureEstimator()
    estimate = estimator.estimate(times, values, finger=True, motion_g=0.02)
    assert estimate.status == "OK"
    assert 40 < estimate.sbp < 250 and 20 < estimate.dbp < 150

    assert estimator.estimate(times, values, finger=False, motion_g=0.02).status == "POOR_SIGNAL"
    assert estimator.estimate(np.empty(0), np.empty(0), finger=True, motion_g=None).status == "NO_WAVEFORM"
    label = estimator.label()
    assert label["experimental"] is True
    assert "sbp_mae" in label


def test_missing_model_is_reported_not_faked(tmp_path):
    estimator = BloodPressureEstimator(model_path=tmp_path / "missing.joblib", metrics_path=tmp_path / "none.json")
    times, values = _wave(31)
    estimate = estimator.estimate(times, values, finger=True, motion_g=0.0)
    assert estimate.status == "NO_MODEL"
    assert estimate.sbp is None and estimate.dbp is None
