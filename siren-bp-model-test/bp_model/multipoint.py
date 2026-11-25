"""Multi-point calibration: use every cuff reading taken so far.

One cuff reading only gives the model an offset. The pulse-shape change that
goes with a BP change differs between patients (in MIMIC-BP the direction
even flips for some), so after a second reading SIREN learns how strongly,
and in which direction, this patient's predictions should be trusted.

  1. Multi-anchor. Each stored cuff reading i gives its own estimate
       est_i = cuff_i + gain * model(f_now, f_cuff_i)
     and the final estimate is their mean. Averaging several anchors cancels
     part of each anchor's error.
  2. Patient gain. Between any two stored cuffs i, k the model predicted a
     change d_ik and the cuffs measured y_ik. The gain is a ridge fit of y
     on d, pulled towards 1 (the population model) by lam:
       gain = (sum(d * y) + lam * 1) / (sum(d * d) + lam)
     With one cuff there are no pairs and gain = 1. More cuffs, or larger
     disagreements, move it away from 1. Clipped to [GAIN_MIN, GAIN_MAX].

Idea from two-point calibration of cuffless BP (Mukkamala et al., IEEE TBME
2015), applied to the model output rather than to pulse transit time.
"""
from __future__ import annotations

import numpy as np

GAIN_MIN, GAIN_MAX = -1.0, 3.0


def fit_gain(d: np.ndarray, y: np.ndarray, lam: float) -> float:
    d = np.asarray(d, dtype=float)
    y = np.asarray(y, dtype=float)
    if d.size == 0 or not np.isfinite(lam):
        return 1.0
    g = (np.sum(d * y) + lam) / (np.sum(d * d) + lam)
    return float(np.clip(g, GAIN_MIN, GAIN_MAX))


def pair_arrays(cuff_vals: np.ndarray, pred_between: np.ndarray):
    """cuff_vals (n,), pred_between (n, n) with pred_between[i, k] = model
    change from cuff i to cuff k. Returns the off-diagonal d and y."""
    n = len(cuff_vals)
    i, k = np.where(~np.eye(n, dtype=bool))
    return pred_between[i, k], cuff_vals[k] - cuff_vals[i]


def combine(cuff_vals: np.ndarray, pred_now: np.ndarray, gain: float) -> np.ndarray:
    """cuff_vals (n,), pred_now (n, m) change predicted from each cuff to m
    windows. Returns the m estimates."""
    return np.mean(cuff_vals[:, None] + gain * pred_now, axis=0)
