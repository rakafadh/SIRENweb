"""Turn MAX30102 raw IR into the 30 s, 125 Hz window the model expects.

prepare_window is copied from siren_app/app/services/bp_service.py.
parse_waveform_log reads the firmware's W lines from a saved serial log.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

FS_MODEL = 125.0
WINDOW_S = 30.0
WINDOW_SAMPLES = int(FS_MODEL * WINDOW_S)  # 3750
MIN_COVERAGE_S = 28.0
MAX_GAP_S = 0.5
DEFAULT_REFERENCE = {"mean": 1.6717, "std": 0.4202}


def prepare_window(times_ms: np.ndarray, values: np.ndarray, reference: dict[str, float] = DEFAULT_REFERENCE):
    """Returns (window or None, reason code, seconds of signal available)."""
    times_ms = np.asarray(times_ms, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
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


# W,<ms of block's last sample>,<fs>,<ir_1>,...,<ir_n>. Anything before the W
# (PlatformIO monitor timestamps, "> ") is ignored.
_W_LINE = re.compile(r"W,(\d+),([\d.]+),([\d,]+)")


def parse_waveform_log(path: str | Path):
    """Read every W line of a serial log. Returns (times_ms, ir) as arrays.

    Each sample gets a timestamp spaced 1/fs back from the block's end time,
    which is how siren_app rebuilds the waveform.
    """
    times, values = [], []
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        m = _W_LINE.search(line)
        if not m:
            continue
        end_ms = float(m.group(1))
        fs = float(m.group(2))
        samples = [int(v) for v in m.group(3).strip(",").split(",") if v]
        if fs <= 0 or not samples:
            continue
        n = len(samples)
        for i, v in enumerate(samples):
            times.append(end_ms - (n - 1 - i) * 1000.0 / fs)
            values.append(v)
    t = np.asarray(times)
    v = np.asarray(values, dtype=np.float64)
    if t.size:
        order = np.argsort(t, kind="stable")
        t, v = t[order], v[order]
    return t, v
