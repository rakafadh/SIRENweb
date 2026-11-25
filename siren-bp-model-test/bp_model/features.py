"""Feature extraction, feature_version signal_summary_v1.

Copied verbatim from siren_app/app/services/bp_service.py. Do not edit unless
you retrain the model: the saved forest expects exactly these 36 numbers in
exactly this order.
"""
from __future__ import annotations

import numpy as np

FEATURE_NAMES = [
    "mean", "std", "min", "max", "median", "ptp", "rms", "mean_abs",
    "mean_abs_diff", "max_abs_diff", "std_diff", "mean_abs_centered",
    "mean_abs_diff2", "sum_abs", "energy", "n_above_median", "n_below_median",
    "frac_pos", "frac_neg", "zero_cross_rate",
    "linspace_1", "linspace_2", "linspace_3", "linspace_4",
    "linspace_5", "linspace_6", "linspace_7", "linspace_8",
    "fft_top6_1", "fft_top6_2", "fft_top6_3", "fft_top6_4", "fft_top6_5", "fft_top6_6",
    "dominant_freq_hz", "low_band_ratio",
]


def extract_features(signal: np.ndarray) -> np.ndarray:
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

    # Known bug kept on purpose: this is a min-to-max ramp, not a downsample.
    # The model was trained with it, so removing it here would break inference.
    downsampled = np.asarray(np.linspace(sig.min(), sig.max(), num=8))
    feature_list.extend(downsampled.tolist())
    feature_list.extend(spectral)
    return np.asarray(feature_list, dtype=np.float64)
