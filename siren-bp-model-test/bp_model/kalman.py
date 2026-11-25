"""Kalman filter fusion of cuff readings and PPG estimates (SBP and DBP).

State   x = true BP (SBP, DBP), modelled as a random walk:
            x(t + dt) = x(t) + w,   Var(w) = q * dt   (q in mmHg^2 per minute)
Cuff    z = x + v,   Var(v) = cuff_sd^2              (accurate, rare)
PPG     z = cuff-anchored model estimate,
            Var = (s0 + s1 * minutes since the anchor cuff)^2 * r_scale
            (the model gets worse the longer it has been since the cuff,
            fitted on validation data)

Every window the filter returns the fused estimate and its uncertainty
(sigma, mmHg). Sigma is small right after a cuff and grows while the PPG
model drifts, so it can drive two things a fixed rule cannot:
  * a cautious NEWS2 score, taken from the estimate minus z * sigma, so a
    patient near a band edge is scored on the worse side, and
  * an uncertainty cuff trigger: take a cuff when the cautious score is worse
    than the score of the last cuff.

The PPG estimate already contains the anchor cuff's error, so it is not truly
independent of the cuff update; q and r_scale are tuned on validation data,
which absorbs most of that. Sigma is checked for coverage on the test set.
"""
from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class KalmanConfig:
    q: float = 4.0            # process noise, mmHg^2 per minute (SBP); DBP uses q * q_dbp_ratio
    q_dbp_ratio: float = 0.3
    cuff_sd: float = 6.0      # cuff error SD, mmHg
    s0: float = 5.0           # PPG model error SD at the cuff, mmHg (SBP)
    s1: float = 0.8           # extra error SD per minute since the cuff (SBP)
    dbp_ratio: float = 0.55   # DBP model error SD relative to SBP
    r_scale: float = 1.0      # multiplies the PPG variance

    def to_dict(self):
        return asdict(self)


class BpKalman:
    """Online filter. Call cuff() when a cuff reading arrives and ppg() for each
    PPG window; both return (estimate [SBP, DBP], sigma [SBP, DBP])."""

    def __init__(self, cfg: KalmanConfig | None = None):
        self.cfg = cfg or KalmanConfig()
        self.x = None
        self.P = None
        self.t = None

    def _predict(self, t_s: float):
        if self.t is not None and t_s > self.t:
            dt = (t_s - self.t) / 60.0
            self.P = self.P + self.cfg.q * np.array([1.0, self.cfg.q_dbp_ratio]) * dt
        self.t = t_s

    def _update(self, z, R):
        K = self.P / (self.P + R)
        self.x = self.x + K * (np.asarray(z, float) - self.x)
        self.P = (1 - K) * self.P

    def cuff(self, t_s: float, value):
        R = np.array([1.0, 1.0]) * self.cfg.cuff_sd ** 2
        if self.x is None:
            self.x, self.P, self.t = np.asarray(value, float).copy(), R.copy(), t_s
        else:
            self._predict(t_s)
            self._update(value, R)
        return self.x.copy(), np.sqrt(self.P)

    def ppg(self, t_s: float, estimate, minutes_since_cuff: float):
        if self.x is None:
            raise RuntimeError("needs a cuff reading first")
        self._predict(t_s)
        sd = self.cfg.s0 + self.cfg.s1 * max(minutes_since_cuff, 0.0)
        R = np.array([sd, sd * self.cfg.dbp_ratio]) ** 2 * self.cfg.r_scale
        self._update(estimate, R)
        return self.x.copy(), np.sqrt(self.P)

    def reset(self):
        self.x = self.P = self.t = None


def fit_error_growth(minutes, err):
    """Least-squares fit of |error| SD against minutes since the cuff:
    sd(m) = s0 + s1 * m, from binned RMS errors. Returns (s0, s1)."""
    minutes, err = np.asarray(minutes), np.asarray(err)
    edges = np.array([0, 1, 2, 3, 5, 7, 10, 12.5, 15.01])
    mids, rms = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (minutes >= lo) & (minutes < hi)
        if m.sum() > 50:
            mids.append(minutes[m].mean())
            rms.append(np.sqrt(np.mean(err[m] ** 2)))
    s1, s0 = np.polyfit(mids, rms, 1)
    return float(max(s0, 1.0)), float(max(s1, 0.0))
