"""Pulse-shape features at the MAX30102's sample rate (feature_version morph25_v1).

Replaces the generic statistics of features.py for the calibrated model.

Pipeline for one 30 s window
  1. Bring the signal to 25 Hz (MIMIC: anti-alias filter + decimate 125 -> 25;
     sensor: already 25 Hz, only inverted so systole points up).
  2. Band-pass 0.5 to 8 Hz, which keeps the pulse shape and removes
     respiration baseline and LED gain level.
  3. Find beats (systolic peaks and the foot before each one).
  4. Upsample every beat 5x with a cubic spline, so timing is not limited to
     the 40 ms sample step, then measure shape.
  5. Median of each beat feature over the window, plus heart-rate statistics
     and a signal-quality index.

Every feature is a time or a ratio. None depends on amplitude in counts, so
LED current, skin tone and finger pressure only matter through shape.

References for the features
  Elgendi et al., npj Digit Med 2019 (review of PPG features for BP)
  Takazawa et al., Hypertension 1998 (second-derivative b/a, d/a)
  Kachuee et al., IEEE TBME 2017 (morphology features on MIMIC)
"""
from __future__ import annotations

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.signal import butter, decimate, find_peaks, sosfiltfilt

_trapz = getattr(np, "trapezoid", None) or np.trapz

FS = 25.0                  # Hz, the device rate
UPSAMPLE = 5               # per-beat spline upsampling
FEATURE_VERSION = "morph25_v1"

_SOS = butter(3, [0.5, 8.0], btype="bandpass", fs=FS, output="sos")

BEAT_FEATURES = [
    "rise_time_ms",        # foot to systolic peak
    "rise_frac",           # rise time / beat duration
    "decay_time_ms",       # peak to next foot
    "width25_ms", "width50_ms", "width75_ms",   # width at 25/50/75% of height
    "width50_frac",        # width50 / beat duration
    "area_ratio",          # area after peak / area before peak
    "max_upslope",         # steepest rise, in beat heights per second
    "max_downslope",       # steepest fall, beat heights per second
    "skew", "kurt",        # shape of the beat as a distribution
    "apg_b_a", "apg_d_a",  # second-derivative ratios (Takazawa)
    "apg_ageing",          # (b - c - d - e) / a, shortened to (b - d) / a at 25 Hz
    "peak_to_end_frac",    # where the beat has fallen to 50% after the peak
]

WINDOW_FEATURES = [
    "hr_bpm", "ibi_sd_ms", "ibi_rmssd_ms", "n_beats", "sqi_template_r",
    "beat_amp_cv",
]

FEATURE_NAMES = (
    [f"{n}_med" for n in BEAT_FEATURES]
    + [f"{n}_iqr" for n in ("rise_time_ms", "width50_ms", "area_ratio", "apg_b_a")]
    + WINDOW_FEATURES
)
N_FEATURES = len(FEATURE_NAMES)


def to_device_rate(window_125: np.ndarray) -> np.ndarray:
    """MIMIC window (125 Hz, upright) -> 25 Hz, the MAX30102 rate."""
    return decimate(np.asarray(window_125, dtype=np.float64), 5, ftype="fir", zero_phase=True)


def _beat_shape(seg: np.ndarray, pk: int) -> list[float] | None:
    """Features of one beat. seg runs foot to next foot, pk is the peak index."""
    n = seg.size
    if n < 6 or pk < 1 or pk >= n - 2:
        return None
    t = np.arange(n) / FS
    cs = CubicSpline(t, seg)
    tf = np.linspace(0, t[-1], (n - 1) * UPSAMPLE + 1)
    y = cs(tf)
    base = np.linspace(y[0], y[-1], y.size)      # remove foot-to-foot tilt
    y = y - base
    ip = int(np.argmax(y))
    amp = y[ip]
    if amp <= 0 or ip == 0 or ip == y.size - 1:
        return None
    yn = y / amp
    dt = tf[1] - tf[0]
    T = tf[-1]

    def width(level):
        above = np.where(yn >= level)[0]
        return (above[-1] - above[0]) * dt * 1000 if above.size else np.nan

    rise = tf[ip]
    decay = T - rise
    a_before = _trapz(yn[: ip + 1], dx=dt)
    a_after = _trapz(yn[ip:], dx=dt)
    d1 = np.gradient(yn, dt)
    d2 = np.gradient(d1, dt)

    # Second derivative waves: a = first maximum (early systole), b = the
    # minimum after it, d = the minimum later in systole/early diastole.
    apg_b_a = apg_d_a = np.nan
    a_i = int(np.argmax(d2[: max(ip, 2)]))
    a = d2[a_i]
    if a > 0:
        after_a = d2[a_i:]
        b_rel = int(np.argmin(after_a[: max(int((ip - a_i) * 1.5), 2)]))
        b = after_a[b_rel]
        b_i = a_i + b_rel
        tail = d2[b_i + UPSAMPLE: min(y.size, ip + int(0.25 * FS * UPSAMPLE))]
        d = tail.min() if tail.size else np.nan
        apg_b_a, apg_d_a = b / a, d / a

    after = np.where(yn[ip:] <= 0.5)[0]
    p_end = (after[0] * dt) / T if after.size else np.nan

    w = yn / max(yn.sum(), 1e-9)
    mu = (w * tf).sum()
    sd = float(np.sqrt(max(float((w * (tf - mu) ** 2).sum()), 0.0)))  # clip rounding below zero (noisy beats)
    skew = (w * ((tf - mu) / sd) ** 3).sum() if sd > 0 else np.nan
    kurt = (w * ((tf - mu) / sd) ** 4).sum() if sd > 0 else np.nan

    return [
        rise * 1000, rise / T, decay * 1000,
        width(0.25), width(0.5), width(0.75), width(0.5) / (T * 1000),
        a_after / max(a_before, 1e-9),
        d1.max(), -d1.min(),
        skew, kurt,
        apg_b_a, apg_d_a, (apg_b_a - apg_d_a) if np.isfinite(apg_b_a) else np.nan,
        p_end,
    ]


def extract(sig25: np.ndarray, fs: float = FS) -> np.ndarray:
    """26 features from a 25 Hz, systole-up PPG window. All NaN when unusable."""
    out = np.full(N_FEATURES, np.nan)
    x = np.asarray(sig25, dtype=np.float64)
    if x.size < fs * 10 or not np.all(np.isfinite(x)) or np.std(x) < 1e-9:
        return out
    x = sosfiltfilt(_SOS, x)
    x = (x - x.mean()) / x.std()

    peaks, _ = find_peaks(x, distance=int(0.3 * fs), prominence=0.4)
    if peaks.size < 6:
        return out
    # Foot = minimum between consecutive peaks
    feet = [int(np.argmin(x[peaks[i - 1]:peaks[i]])) + peaks[i - 1] for i in range(1, peaks.size)]
    feet = np.array(feet)

    beats, segs, amps = [], [], []
    for i in range(feet.size - 1):
        f0, f1 = feet[i], feet[i + 1]
        dur = (f1 - f0) / fs
        if not 0.27 <= dur <= 2.0:
            continue
        pk = peaks[i + 1] - f0
        seg = x[f0:f1 + 1]
        r = _beat_shape(seg, pk)
        if r is None:
            continue
        beats.append(r)
        segs.append(np.interp(np.linspace(0, seg.size - 1, 50), np.arange(seg.size), seg))
        amps.append(x[peaks[i + 1]] - x[f0])
    if len(beats) < 5:
        return out
    B = np.array(beats, dtype=np.float64)

    # Signal quality: mean correlation of each beat with the median beat
    S = np.array(segs)
    tmpl = np.median(S, axis=0)
    sqi = float(np.nanmean([np.corrcoef(s, tmpl)[0, 1] for s in S]))

    # Drop beats that do not look like the template (motion, ectopics)
    keep = np.array([np.corrcoef(s, tmpl)[0, 1] >= 0.8 for s in S])
    if keep.sum() >= 5:
        B = B[keep]

    ibi = np.diff(peaks) / fs * 1000
    ibi = ibi[(ibi > 270) & (ibi < 2000)]
    med = np.nanmedian(B, axis=0)

    def iqr(col):
        v = B[:, BEAT_FEATURES.index(col)]
        return float(np.nanpercentile(v, 75) - np.nanpercentile(v, 25))

    amps = np.array(amps)
    vals = list(med) + [iqr(c) for c in ("rise_time_ms", "width50_ms", "area_ratio", "apg_b_a")] + [
        60000 / np.median(ibi) if ibi.size else np.nan,
        float(np.std(ibi)) if ibi.size > 2 else np.nan,
        float(np.sqrt(np.mean(np.diff(ibi) ** 2))) if ibi.size > 3 else np.nan,
        float(B.shape[0]),
        sqi,
        float(np.std(amps) / np.mean(amps)) if np.mean(amps) > 0 else np.nan,
    ]
    out[:] = vals
    return out


def extract_mimic(window_125: np.ndarray) -> np.ndarray:
    return extract(to_device_rate(window_125))


def extract_sensor(raw_ir_25: np.ndarray) -> np.ndarray:
    """Raw MAX30102 IR at about 25 Hz. IR dips at systole, so invert first."""
    return extract(-np.asarray(raw_ir_25, dtype=np.float64))
