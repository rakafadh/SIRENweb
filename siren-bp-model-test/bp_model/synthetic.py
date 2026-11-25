"""Synthetic PPG for smoke tests. Not physiological data: use it to check the
code runs and how the model reacts to shape changes, never to judge accuracy."""
from __future__ import annotations

import numpy as np


def ppg_pulse(phase: np.ndarray, notch: float = 0.25, rise: float = 0.15) -> np.ndarray:
    """One beat on phase 0..1: fast systolic rise, slower decay, dicrotic bump."""
    sys = np.exp(-((phase - rise) ** 2) / (2 * (rise / 2.2) ** 2))
    decay = np.where(phase > rise, np.exp(-(phase - rise) / 0.35), 0.0)
    dic = notch * np.exp(-((phase - 0.45) ** 2) / (2 * 0.05 ** 2))
    return 0.6 * sys + 0.4 * decay + dic


def upright_ppg(seconds: float, fs: float, hr_bpm: float = 75, rr_bpm: float = 15,
                notch: float = 0.25, rise: float = 0.15, noise: float = 0.01,
                seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Upright PPG (like MIMIC pleth). Returns (t_seconds, signal)."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1 / fs)
    inst_hr = hr_bpm * (1 + 0.03 * np.sin(2 * np.pi * rr_bpm / 60 * t))
    phase = np.cumsum(inst_hr / 60 / fs) % 1.0
    sig = ppg_pulse(phase, notch, rise)
    sig *= 1 + 0.08 * np.sin(2 * np.pi * rr_bpm / 60 * t)          # amplitude (RIAV)
    sig += 0.15 * np.sin(2 * np.pi * rr_bpm / 60 * t)               # baseline (RIIV)
    sig += noise * rng.standard_normal(t.size)
    return t, sig


def max30102_ir(seconds: float, fs: float = 25.0, dc: float = 120000, ac_frac: float = 0.01,
                **kw) -> tuple[np.ndarray, np.ndarray]:
    """Raw MAX30102-style IR counts: large DC, small AC, inverted at systole.
    Returns (times_ms, counts)."""
    t, sig = upright_ppg(seconds, fs, **kw)
    counts = dc - dc * ac_frac * sig
    return t * 1000.0, np.round(counts)
