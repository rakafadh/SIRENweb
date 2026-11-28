"""Which PPG windows are clean enough to use (the signal quality gate).

The quality score of a window (features_v2, "sqi_template_r") is the mean
correlation of EVERY beat with the median beat. One or two beats spoilt by a
finger twitch drag that mean down, so the whole 30 s window is rejected, even
though the features themselves are computed from the clean beats only
(features_v2 already drops beats that correlate < 0.8 with the template).

The strict gate (the one the VitalDB results were obtained with):
    estimate   score >= 0.80
    calibrate  score >= 0.90 (and >= 15 beats, checked in calibrated.py)

The rescue gate also accepts a window whose score is lower but in which most
beats are clean:
    estimate   score >= rescue_est_sqi and clean share >= rescue_est_frac
    calibrate  score >= rescue_cal_sqi and clean share >= rescue_cal_frac
Clean share = clean beats kept (n_beats) / beats expected from the heart rate
over the window. It uses only stored features, so the same rule can be
replayed on the VitalDB feature caches (scripts/quality_gate_check.py), which is
how its thresholds are chosen and checked. Model inputs are unchanged: the
gate only decides which windows are used.
"""
from dataclasses import dataclass

import numpy as np

I_HR, I_NB, I_SQI = -6, -3, -2          # hr_bpm, n_beats (clean beats kept), sqi_template_r


@dataclass
class QualityGate:
    est_sqi: float = 0.80
    cal_sqi: float = 0.90
    rescue: bool = False
    rescue_est_sqi: float = 0.60
    rescue_est_frac: float = 0.75
    rescue_cal_sqi: float = 0.75
    rescue_cal_frac: float = 0.85
    window_s: float = 30.0


def clean_frac(F, window_s: float = 30.0):
    """Share of the expected beats that were clean enough to keep (0-1)."""
    F = np.asarray(F, dtype=np.float64)
    with np.errstate(all="ignore"):
        fr = F[..., I_NB] / (F[..., I_HR] / 60.0 * window_s)
    return np.clip(np.nan_to_num(fr, nan=0.0, posinf=0.0), 0.0, 1.0)


def _sqi(F):
    return np.nan_to_num(np.asarray(F, dtype=np.float64)[..., I_SQI], nan=0.0)


def ok_estimate(F, g: QualityGate):
    s = _sqi(F)
    ok = s >= g.est_sqi
    if g.rescue:
        ok = ok | ((s >= g.rescue_est_sqi) & (clean_frac(F, g.window_s) >= g.rescue_est_frac))
    return ok


def ok_calibrate(F, g: QualityGate):
    s = _sqi(F)
    ok = s >= g.cal_sqi
    if g.rescue:
        ok = ok | ((s >= g.rescue_cal_sqi) & (clean_frac(F, g.window_s) >= g.rescue_cal_frac))
    return ok


def effective_sqi(F, g: QualityGate):
    """Score mapped onto the strict thresholds, for code that compares a score
    with 0.80 / 0.90 (the VitalDB simulator): a rescued window is raised to
    exactly the threshold it now passes."""
    q = _sqi(F).copy()
    if g.rescue:
        q = np.where(ok_estimate(F, g) & (q < g.est_sqi), g.est_sqi, q)
        q = np.where(ok_calibrate(F, g) & (q < g.cal_sqi), g.cal_sqi, q)
    return q
