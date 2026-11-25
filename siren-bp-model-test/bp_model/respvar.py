"""Respiratory variation of the finger PPG (a pleth variability index analogue).

With every breath, intrathoracic pressure changes the amount of blood the
heart ejects, so PPG pulse height swings. The swing grows when circulating
volume falls (hypovolaemia), which is why pleth variability (PVI, delta-POP)
is used to judge fluid status, best studied in ventilated patients
(Cannesson et al.). For SIREN it is a candidate early sign of bleeding.

Every feature is a ratio or a fraction, so the PPG gain does not matter
(VitalDB rescales its PLETH and the MAX30102 gain varies). From a >= 20 s
window at >= 50 Hz, systole up:
  amp_var        (P90 - P10) / median of beat amplitudes (peak minus preceding foot)
  amp_resp_frac  share of the beat-amplitude series' power at 0.1-0.5 Hz (breathing band)
  base_var       (P90 - P10) of the 0.05-0.5 Hz baseline / median beat amplitude
  ibi_var        (P90 - P10) / median of beat intervals (respiratory sinus arrhythmia)
  width_var      (P90 - P10) / median of beat half-height widths
N_RESP = 5; all NaN when fewer than 8 good beats.
"""
import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt

N_RESP = 5
RESP_NAMES = ["amp_var", "amp_resp_frac", "base_var", "ibi_var", "width_var"]


def _spread(x):
    x = np.asarray(x, float)
    m = np.median(x)
    return float((np.percentile(x, 90) - np.percentile(x, 10)) / m) if m > 0 else np.nan


def resp_features(ppg: np.ndarray, fs: float) -> np.ndarray:
    out = np.full(N_RESP, np.nan)
    x = np.asarray(ppg, float)
    if x.size < fs * 20 or not np.all(np.isfinite(x)) or np.std(x) < 1e-9:
        return out
    pulse = sosfiltfilt(butter(3, [0.5, 8.0], btype="band", fs=fs, output="sos"), x)
    base = sosfiltfilt(butter(2, [0.05, 0.5], btype="band", fs=fs, output="sos"), x)
    pk, _ = find_peaks(pulse, distance=int(0.33 * fs), prominence=0.3 * np.std(pulse))
    if pk.size < 9:
        return out
    amps, ts, widths = [], [], []
    for a, b in zip(pk[:-1], pk[1:]):
        foot = a + int(np.argmin(pulse[a:b]))
        if foot >= b:
            continue
        amp = pulse[b] - pulse[foot]
        if amp <= 0:
            continue
        half = pulse[foot] + amp / 2
        above = np.where(pulse[foot:min(b + int(0.6 * fs), x.size)] >= half)[0]
        amps.append(amp)
        ts.append(b / fs)
        widths.append(above.size / fs if above.size else np.nan)
    amps, ts, widths = np.array(amps), np.array(ts), np.array(widths)
    if amps.size < 8:
        return out
    keep = np.abs(amps - np.median(amps)) <= 4 * (np.median(np.abs(amps - np.median(amps))) + 1e-9)
    amps, ts, widths = amps[keep], ts[keep], widths[keep]
    if amps.size < 8:
        return out
    # breathing-band share of the beat-amplitude series, resampled to 4 Hz
    g = np.arange(ts[0], ts[-1], 0.25)
    frac = np.nan
    if g.size >= 32:
        a4 = np.interp(g, ts, amps) - np.mean(amps)
        f = np.fft.rfftfreq(g.size, 0.25)
        p = np.abs(np.fft.rfft(a4 * np.hanning(g.size))) ** 2
        tot = p[(f > 0.03) & (f < 2.0)].sum()
        frac = float(p[(f >= 0.1) & (f <= 0.5)].sum() / tot) if tot > 0 else np.nan
    med_amp = np.median(amps)
    out[:] = [_spread(amps), frac,
              float((np.percentile(base, 90) - np.percentile(base, 10)) / med_amp) if med_amp > 0 else np.nan,
              _spread(np.diff(ts)), _spread(widths[np.isfinite(widths)]) if np.isfinite(widths).sum() >= 8 else np.nan]
    return out


def synthetic_ppg(seconds=30.0, fs=100.0, hr=75.0, rr=12.0, resp_depth=0.1, seed=0):
    """PPG whose pulse height swings by +-resp_depth with breathing."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    beats = np.arange(0.3, seconds, 60.0 / hr)
    x = np.zeros_like(t)
    for tb in beats:
        a = 1.0 + resp_depth * np.sin(2 * np.pi * rr / 60 * tb)
        u = t - tb
        x += a * np.where(u > 0, (u / 0.12) ** 2 * np.exp(-u / 0.12) * np.exp(2) / 4, 0.0)
    return x + rng.normal(0, 0.005, t.size)
