"""Pulse arrival time (PAT) from one ECG lead and a finger PPG on one clock.

PAT = time from the ECG R peak to the arrival of that beat's pulse at the
finger. It is pulse transit time plus the pre-ejection period, so it shortens
when arterial pressure rises (stiffer, faster-conducting arteries) and
lengthens when it falls (Mukkamala et al., IEEE TBME 2015). Typical finger
values are 150-400 ms; a 10 mmHg change moves it by roughly 5-20 ms, so both
signals need >= 200 Hz.

Per beat three PPG landmarks are found after each R peak:
  foot   intersecting-tangent point (tangent at the steepest upstroke meets
         the pre-beat minimum), the usual PAT definition
  slope  steepest upstroke (max first derivative), the most robust landmark
  peak   systolic peak
Window features (N_PAT = 6):
  [pat_foot_ms, pat_slope_ms, pat_peak_ms, pat_slope_iqr_ms, valid_fraction, hr_ecg_bpm]
All NaN when the window has too few good beats.
"""
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

N_PAT = 6
PAT_NAMES = ["pat_foot_ms", "pat_slope_ms", "pat_peak_ms", "pat_slope_iqr_ms", "pat_valid_frac", "hr_ecg_bpm"]


def _band(x, fs, lo, hi, order=2):
    b, a = butter(order, [lo / (fs / 2), min(hi / (fs / 2), 0.99)], btype="band")
    return filtfilt(b, a, x)


def r_peaks(ecg: np.ndarray, fs: float) -> np.ndarray:
    """R peak sample indices (simplified Pan-Tompkins): band-pass 5-20 Hz,
    derivative, square, 150 ms moving integration, adaptive threshold, 300 ms
    refractory, then the R location is refined to the band-passed maximum."""
    x = np.asarray(ecg, dtype=np.float64)
    if x.size < fs * 3 or not np.all(np.isfinite(x)) or np.std(x) < 1e-9:
        return np.array([], dtype=int)
    bp = _band(x, fs, 5.0, 20.0)
    if np.abs(np.percentile(bp, 0.5)) > np.abs(np.percentile(bp, 99.5)):   # inverted lead
        bp = -bp
    d = np.gradient(bp) ** 2
    w = max(int(0.15 * fs), 1)
    mwi = np.convolve(d, np.ones(w) / w, mode="same")
    pk, _ = find_peaks(mwi, height=0.3 * np.percentile(mwi, 99), distance=int(0.3 * fs))
    half = int(0.08 * fs)
    out = []
    for p in pk:
        a, b = max(p - half, 0), min(p + half, x.size)
        out.append(a + int(np.argmax(bp[a:b])))
    return np.unique(np.array(out, dtype=int))


def beat_pat(ecg: np.ndarray, ppg: np.ndarray, fs: float):
    """Per-beat PAT (seconds) to foot, steepest upstroke and peak, plus R peaks.
    PPG must be systole-up (rises with each beat)."""
    r = r_peaks(ecg, fs)
    p = np.asarray(ppg, dtype=np.float64)
    nan = np.full(len(r), np.nan)
    if r.size < 3 or not np.all(np.isfinite(p)):
        return r, nan, nan.copy(), nan.copy()
    b, a = butter(2, 8.0 / (fs / 2), btype="low")
    p = filtfilt(b, a, p - np.median(p))
    dp = np.gradient(p) * fs
    foot, slope, peak = nan.copy(), nan.copy(), nan.copy()
    for k, ri in enumerate(r):
        a0, a1 = ri + int(0.08 * fs), ri + int(0.50 * fs)          # upstroke expected 80-500 ms after R
        nxt = r[k + 1] if k + 1 < r.size else p.size
        a1 = min(a1, nxt + int(0.08 * fs), p.size - 1)
        if a1 - a0 < 3:
            continue
        ms = a0 + int(np.argmax(dp[a0:a1]))
        if dp[ms] <= 0:
            continue
        lo = int(np.argmin(p[max(ri, ms - int(0.3 * fs)):ms + 1])) + max(ri, ms - int(0.3 * fs))
        t_foot = ms + (p[lo] - p[ms]) / dp[ms] * fs                  # tangent meets the minimum
        pe = ms + int(np.argmax(p[ms:min(ms + int(0.35 * fs), p.size)]))
        slope[k] = (ms - ri) / fs
        foot[k] = (t_foot - ri) / fs
        peak[k] = (pe - ri) / fs
    ok = (slope > 0.08) & (slope < 0.5) & (foot > 0.05) & (foot < slope + 1e-9) & (peak > slope)
    foot[~ok] = slope[~ok] = peak[~ok] = np.nan
    return r, foot, slope, peak


def pat_features(ecg: np.ndarray, ppg: np.ndarray, fs: float, min_beats: int = 8) -> np.ndarray:
    """6 window features (see module docstring); NaN when unusable."""
    out = np.full(N_PAT, np.nan)
    r, foot, slope, peak = beat_pat(ecg, ppg, fs)
    if r.size < 3:
        return out
    rr = np.diff(r) / fs
    hr = 60.0 / np.median(rr)
    good = np.isfinite(slope)
    if good.sum() < min_beats:
        out[4], out[5] = good.mean() if r.size else 0.0, hr
        return out
    s = slope[good]
    keep = np.abs(s - np.median(s)) <= 3 * (np.median(np.abs(s - np.median(s))) + 0.002)   # drop outlier beats
    s = s[keep]
    out[:] = [np.nanmedian(foot[good][keep]) * 1000, np.median(s) * 1000, np.nanmedian(peak[good][keep]) * 1000,
              (np.percentile(s, 75) - np.percentile(s, 25)) * 1000, keep.sum() / r.size, hr]
    return out


def synthetic_ecg_ppg(seconds=30.0, fs=250.0, hr=72.0, pat_s=0.25, noise=0.02, seed=0):
    """Test signal: Gaussian R spikes and a PPG pulse whose upstroke starts
    pat_s after each R peak (pat_s may be an array per beat)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    beats = np.arange(0.5, seconds - 0.5, 60.0 / hr)
    pats = np.broadcast_to(np.asarray(pat_s, float), beats.shape)
    ecg = np.zeros_like(t)
    ppg = np.zeros_like(t)
    for tb, pt in zip(beats, pats):
        ecg += np.exp(-0.5 * ((t - tb) / 0.012) ** 2) - 0.15 * np.exp(-0.5 * ((t - tb - 0.04) / 0.02) ** 2)
        u = t - (tb + pt)
        ppg += np.where(u > 0, (u / 0.12) ** 2 * np.exp(-u / 0.12) * np.exp(2) / 4, 0.0)
    return ecg + rng.normal(0, noise, t.size), ppg + rng.normal(0, noise * 0.2, t.size), beats
