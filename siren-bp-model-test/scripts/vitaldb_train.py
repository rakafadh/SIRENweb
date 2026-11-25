"""Train and test the calibrated model on time-ordered VitalDB data (v2).

    python scripts/vitaldb_train.py

Run scripts/vitaldb_download.py first. Uses vitaldb_cache/*.npz.

Improvements over v1, each measured separately (section 1 of the report)
  A. v1 model: [f_now, f_now - f_cuff], trained on all windows
  B. + clean windows only: calibration windows need SQI >= 0.90 and estimated
       windows SQI >= 0.80 (SIREN refuses worse windows anyway)
  C. + recent trend and time since cuff: adds f_now - mean(f over the last
       30-120 s) and minutes since the cuff (layout v2, 79 inputs)
  D. + output smoothing: mean of the estimates of the last --smooth-s seconds
       (causal, chosen on validation)
  E. + ensemble: mean of gradient boosting and random forest
  All variants are scored on the same clean test pairs.

Then, with the final pipeline
  2. error against minutes since the cuff,
  3. second-cuff timing (none, 2, 5, 10 min) with equal or time-weighted
     averaging of cuffs (half-life chosen on validation),
  4. drop and hypotension alerts tuned on validation (<= --max-false-alarm %).

Patients are split by VitalDB subjectid (70/15/15). Results:
results/vitaldb_comparison.md, model/bp_calibrated_vitaldb.joblib,
model/alert_settings_vitaldb.json.
"""
import _path  # noqa: F401
import os
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))   # silences a harmless Windows warning
import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import make_pipeline

from bp_model.calibrated import (RECENT_FROM_S, RECENT_TO_S, CalibrationConfig, Ensemble, pair_features,
                                 pair_features_v2)
from bp_model.evaluation import alert_curve, auroc, metrics, table, threshold_for_false_alarm
from bp_model.features_v2 import FEATURE_VERSION, N_FEATURES
from bp_model.kalman import BpKalman, KalmanConfig, fit_error_growth

ROOT = Path(__file__).resolve().parent.parent
SQI = N_FEATURES - 2          # column of the template-correlation quality index


# ------------------------------------------------------------------- data
def load_cases(cache: Path) -> list[dict]:
    cases = []
    for p in sorted(cache.glob("*.npz")):
        z = np.load(p, allow_pickle=False)
        if "empty" in z.files or str(z["version"]) != FEATURE_VERSION or len(z["t"]) < 20:
            continue
        t, F = z["t"], z["F"]
        c = {"caseid": int(z["caseid"]), "subject": int(z["subjectid"]), "t": t, "F": F, "L": z["L"],
             "nibp": z["nibp"], "sqi": np.nan_to_num(F[:, SQI], nan=0.0)}
        c["Frec"] = recent_features(c, 0.80)
        cases.append(c)
    return cases


def recent_features(c, min_sqi):
    """Mean features of clean windows ending 30-120 s before each window."""
    t, F, ok = c["t"], c["F"], c["sqi"] >= min_sqi
    out = np.full_like(F, np.nan)
    lo = np.searchsorted(t, t - RECENT_FROM_S, side="left")
    hi = np.searchsorted(t, t - RECENT_TO_S, side="right")
    for j in range(len(t)):
        k = np.arange(lo[j], hi[j])
        k = k[ok[k]]
        if k.size:
            out[j] = np.nanmean(F[k], axis=0)
    return out


def split_cases(cases, seed):
    subjects = sorted({c["subject"] for c in cases})
    order = np.random.default_rng(seed).permutation(len(subjects))
    n = len(subjects)
    tr = {subjects[i] for i in order[:int(0.7 * n)]}
    va = {subjects[i] for i in order[int(0.7 * n):int(0.85 * n)]}
    out = {"train": [], "val": [], "test": []}
    for c in cases:
        out["train" if c["subject"] in tr else "val" if c["subject"] in va else "test"].append(c)
    return out


def pair_index(cases, cal_every_s, max_gap_s, max_targets, rng, clean, cal_sqi, min_sqi):
    """Rows (case, cal window, target window). Targets 15 s to max_gap later."""
    rows = []
    for ci, c in enumerate(cases):
        t, q = c["t"], c["sqi"]
        next_cal = -np.inf
        for i in range(len(t)):
            if t[i] < next_cal or (clean and q[i] < cal_sqi):
                continue
            next_cal = t[i] + cal_every_s
            j = np.where((t > t[i] + 15) & (t <= t[i] + max_gap_s))[0]
            if clean:
                j = j[q[j] >= min_sqi]
            if j.size == 0:
                continue
            if max_targets and j.size > max_targets:
                j = np.sort(rng.choice(j, max_targets, replace=False))
            rows += [(ci, i, k) for k in j]
    return np.array(rows, dtype=int).reshape(-1, 3)


def build(cases, idx, layout):
    X, Y, CAL, GAP = [], [], [], []
    for ci, i, j in idx:
        c = cases[ci]
        F, L, t = c["F"], c["L"], c["t"]
        if layout == "v1":
            X.append(pair_features(F[j], F[i], L[i, 0], L[i, 1])[:2 * N_FEATURES])
        else:
            X.append(pair_features_v2(F[j], F[i], c["Frec"][j], (t[j] - t[i]) / 60))
        Y.append(L[j] - L[i]); CAL.append(L[i]); GAP.append(t[j] - t[i])
    return np.array(X), np.array(Y), np.array(CAL), np.array(GAP)


# ----------------------------------------------------------------- models
def hgb(seed):
    return MultiOutputRegressor(HistGradientBoostingRegressor(
        max_iter=500, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.1, random_state=seed))


def rf(seed):
    return make_pipeline(SimpleImputer(strategy="median"), RandomForestRegressor(
        n_estimators=200, max_depth=14, min_samples_leaf=20, max_features=0.4, n_jobs=-1, random_state=seed))


def band_weights(Y, CAL, mode="inverse", cap=10.0):
    """Band-balanced training weights (after Lim et al. 2025): pairs whose true
    SBP falls in a rare NEWS2 band count more. inverse = N / N_band, sqrt =
    its square root; normalised to mean 1, capped."""
    band = news2_band(CAL[:, 0] + Y[:, 0])
    cnt = np.bincount(band, minlength=4).astype(float)
    w = cnt.sum() / np.maximum(cnt[band], 1.0)
    if mode == "sqrt":
        w = np.sqrt(w)
    w = w / w.mean()
    return np.minimum(w, cap)


def fit(model, X, Y, CAL, w_low, weights=None):
    w = np.where(Y[:, 0] + CAL[:, 0] <= 110, w_low, 1.0) if weights is None else weights
    kw = {f"{model.steps[-1][0]}__sample_weight": w} if hasattr(model, "steps") else {"sample_weight": w}
    return model.fit(X, Y, **kw)


# ----------------------------------------------------------- smoothing
def smooth_pairs(cases, idx, pred, smooth_s):
    """Causal mean over targets of the same calibration within smooth_s seconds."""
    if smooth_s <= 0:
        return pred
    out = pred.copy()
    keys = idx[:, 0] * 100000 + idx[:, 1]
    for key in np.unique(keys):
        rows = np.where(keys == key)[0]
        c = cases[idx[rows[0], 0]]
        tt = c["t"][idx[rows, 2]]
        order = np.argsort(tt)
        rows, tt = rows[order], tt[order]
        for a in range(len(rows)):
            m = (tt > tt[a] - smooth_s) & (tt <= tt[a])
            out[rows[a]] = pred[rows[m]].mean(axis=0)
    return out


# ------------------------------------------------------- protocol simulation
def nearest(t, target, tol=31.0):
    i = int(np.argmin(np.abs(t - target)))
    return i if abs(t[i] - target) <= tol else None


def simulate(cases, model, layout, gap_min, cuff_noise, source, rng, half_life_min=np.inf, smooth_s=0.0,
             horizon_min=15.0, start_every_min=10.0, cal_sqi=0.9, min_sqi=0.8):
    """Two passes: collect every (cuff, target) pair first and predict them in
    ONE batch (calling a random forest thousands of times on tiny batches is
    very slow on Windows), then build the estimates."""
    plans, X = [], []
    for c in cases:
        t, F, L, nibp, q = c["t"], c["F"], c["L"], c["nibp"], c["sqi"]
        t_start = t[0]
        while t_start + horizon_min * 60 <= t[-1]:
            i0 = nearest(t, t_start)
            t_start += start_every_min * 60
            if i0 is None or q[i0] < cal_sqi:
                continue
            times = [t[i0]] + ([t[i0] + gap_min * 60] if gap_min else [])
            cuffs = []
            for tc in times:
                i = nearest(t, tc)
                if i is None or q[i] < cal_sqi:
                    break
                if source == "nibp":
                    if len(nibp) == 0:
                        break
                    k = int(np.argmin(np.abs(nibp[:, 0] - t[i])))
                    if abs(nibp[k, 0] - t[i]) > 60:
                        break
                    val = nibp[k, 1:3]
                else:
                    val = L[i] + rng.normal(0, cuff_noise, 2)
                cuffs.append((t[i], i, val))
            if len(cuffs) != len(times):
                continue
            tgt = np.where((t > t[i0] + 15) & (t <= t[i0] + horizon_min * 60) & (q >= min_sqi))[0]
            tgt = tgt[[all(t[j] != cc[0] for cc in cuffs) for j in tgt]]
            if tgt.size == 0:
                continue
            plans.append((c, cuffs, tgt, len(X)))
            for (tc, ci, v) in cuffs:
                for j in tgt:
                    if layout == "v1":
                        X.append(pair_features(F[j], F[ci], v[0], v[1])[:2 * N_FEATURES])
                    else:
                        X.append(pair_features_v2(F[j], F[ci], c["Frec"][j], (t[j] - tc) / 60))
    rows = {k: [] for k in ("true", "hold", "hold_mean", "model_latest", "model_multi", "latest")}
    if not plans:
        return {k: np.array(v) for k, v in rows.items()}
    P_all = model.predict(np.array(X))

    for c, cuffs, tgt, start in plans:
        t, L = c["t"], c["L"]
        P = P_all[start:start + len(cuffs) * tgt.size].reshape(len(cuffs), tgt.size, 2).copy()
        tt = t[tgt]
        if smooth_s > 0:                       # causal smoothing of each cuff's change series
            for k, (tc, _, _) in enumerate(cuffs):
                Pk = P[k].copy()
                for a in range(tgt.size):
                    if tt[a] <= tc:            # this cuff is not taken yet at that moment
                        continue
                    m = (tt > max(tt[a] - smooth_s, tc)) & (tt <= tt[a])
                    Pk[a] = P[k][m].mean(axis=0)
                P[k] = Pk
        for n_j, j in enumerate(tgt):
            avail = [k for k, cc in enumerate(cuffs) if cc[0] < t[j]]
            if not avail:
                continue
            vals = np.array([cuffs[k][2] for k in avail])
            age = np.array([(t[j] - cuffs[k][0]) / 60 for k in avail])
            w = 0.5 ** (age / half_life_min) if np.isfinite(half_life_min) else np.ones(len(avail))
            w = w / w.sum()
            est = np.array([vals[m] + P[k, n_j] for m, k in enumerate(avail)])
            rows["true"].append(L[j]); rows["latest"].append(vals[-1])
            rows["hold"].append(vals[-1]); rows["hold_mean"].append(vals.mean(axis=0))
            rows["model_latest"].append(vals[-1] + P[avail[-1], n_j])
            rows["model_multi"].append((w[:, None] * est).sum(axis=0))
    return {k: np.array(v) for k, v in rows.items()}


def news2_band(sbp):
    sbp = np.asarray(sbp)
    return np.select([sbp <= 90, sbp <= 100, sbp <= 110, sbp <= 219], [3, 2, 1, 0], 3)


_PAIR_CACHE: dict = {}   # model change predictions per case, shared by every schedule (they do not depend on it)


def schedule_sim(cases, model, base_every_min, trigger_mmhg, cuff_noise, rng, half_life_min=5.0,
                 horizon_min=15.0, start_every_min=10.0, min_gap_min=2.0, cal_sqi=0.9, min_sqi=0.8,
                 max_anchors=3, drop_only=False, band_margin=0.0, kalman=None, score_z=0.0,
                 unc_trigger=False, fixed_margin=0.0, extra=False, confirm_mmhg=None, confirm_after_min=2.0,
                 cuff_delay_min=0.0, pending_display="live"):
    """Cuff schedule test: cuffs every base_every_min, plus (when trigger_mmhg is
    set) an extra cuff as soon as the estimate moves >= trigger_mmhg from the
    last cuff or crosses a NEWS2 SBP band, at most one per min_gap_min.

    Fast: for each start, the model change from every candidate cuff window to
    every later window is predicted in one batch, then the schedule is replayed.
    kalman: a KalmanConfig to fuse cuffs and PPG estimates (bp_model.kalman);
    score_z: NEWS2 score taken from estimate - score_z * sigma (Kalman only);
    fixed_margin: NEWS2 score from estimate - fixed_margin (no Kalman);
    unc_trigger: extra cuff when that cautious score is worse than the last cuff's.
    confirm_mmhg: confirmatory cuff confirm_after_min later whenever a cuff
    differs from the estimate just before it by more than confirm_mmhg
    (catches cuff errors and sudden real changes); a confirmation never
    triggers another one.
    cuff_delay_min: time from "cuff now" to the reading (crew response plus
    measurement time; an automatic monitor about 0.5-1 min, a crew pressing
    start 1-3 min). Estimates carry on from the old cuffs meanwhile and no new
    request is made while one is pending; the base interval restarts at the
    reading.
    pending_display: what the crew sees while a requested cuff is pending:
    "live" (keep updating the estimate), "hold_estimate" (freeze the last
    estimate and NEWS2 score shown when the cuff was asked for) or
    "hold_cuff" (show the last cuff value). Metrics are computed on what is
    shown.
    Returns true (n,2), estimate (n,2), last cuff (n,2), cuffs per start
    (extra=True also returns a dict with sigma, shown band and minutes since cuff)."""
    T, E, H, ncuff, S, PB, AGE, CID, POS = [], [], [], [], [], [], [], [], []
    guard = None
    if isinstance(score_z, dict):      # classify-then-regress guard: {"clf": classifier, "thr": p threshold}
        guard, score_z = score_z, 0.0
    for c_i, c in enumerate(cases):
        t, F, L, q, R = c["t"], c["F"], c["L"], c["sqi"], c["Frec"]
        starts, ts = [], t[0]
        while ts + horizon_min * 60 <= t[-1]:
            i0 = nearest(t, ts)
            if i0 is not None and q[i0] >= cal_sqi:
                win = np.where((t >= t[i0]) & (t <= ts + horizon_min * 60) & (q >= min_sqi))[0]
                if win.size > 2:
                    starts.append(win)
            ts += start_every_min * 60
        if not starts:
            continue
        key = (id(model), c.get("caseid", id(c)), horizon_min, start_every_min, cal_sqi, min_sqi)
        if key in _PAIR_CACHE:
            spans, P_all = _PAIR_CACHE[key]
        else:
            X, spans = [], []
            for win in starts:                      # anchor a -> target b, a < b
                a_idx, b_idx = np.triu_indices(win.size, k=1)
                spans.append((len(X), a_idx, b_idx))
                for a, b in zip(a_idx, b_idx):
                    X.append(pair_features_v2(F[win[b]], F[win[a]], R[win[b]], (t[win[b]] - t[win[a]]) / 60))
            P_all = model.predict(np.array(X))
            if len(_PAIR_CACHE) > 5000:
                _PAIR_CACHE.clear()
            _PAIR_CACHE[key] = (spans, P_all)
        for win, (off, a_idx, b_idx) in zip(starts, spans):
            n = win.size
            P = np.full((n, n, 2), np.nan)
            P[a_idx, b_idx] = P_all[off:off + a_idx.size]
            cuffs = [(0, L[win[0]] + rng.normal(0, cuff_noise, 2))]   # (position in win, value)
            kf = BpKalman(kalman) if kalman is not None else None
            if kf is not None:
                kf.cuff(t[win[0]], cuffs[0][1])
            next_due = t[win[0]] + base_every_min * 60
            last_est, confirm_at, pending_at = None, None, None
            last_shown, held = None, None

            def confirm_time(tb_):
                if confirm_mmhg is None or last_est is None or abs(cuffs[-1][1][0] - last_est[0]) <= confirm_mmhg:
                    return None
                return tb_ + confirm_after_min * 60

            def take(b_, tb_, is_confirm=False):
                """Cuff reading at window b_ (the BP at that moment, plus cuff error)."""
                nonlocal next_due, confirm_at, pending_at
                cuffs.append((b_, L[win[b_]] + rng.normal(0, cuff_noise, 2)))
                if kf is not None:
                    kf.cuff(tb_, cuffs[-1][1])
                next_due = tb_ + base_every_min * 60
                confirm_at = None if is_confirm else confirm_time(tb_)
                pending_at = None

            def request(b_, tb_, is_confirm=False):
                """A cuff is asked for now. With cuff_delay_min the reading happens that much later
                (crew response + measurement time); otherwise immediately. Returns True if taken now."""
                nonlocal pending_at, confirm_at, held
                if cuff_delay_min > 0:
                    if pending_at is None:
                        pending_at = tb_ + cuff_delay_min * 60
                        held = (last_est, last_shown) if last_est is not None else None
                        if is_confirm:
                            confirm_at = None
                    return False
                take(b_, tb_, is_confirm)
                return True

            for b in range(1, n):
                tb = t[win[b]]
                if pending_at is not None and tb >= pending_at and q[win[b]] >= cal_sqi:    # delayed cuff arrives
                    take(b, tb)
                    continue
                if pending_at is None and confirm_at is not None and tb >= confirm_at and q[win[b]] >= cal_sqi:
                    if request(b, tb, is_confirm=True):                              # confirmatory cuff
                        continue
                if pending_at is None and tb >= next_due and q[win[b]] >= cal_sqi:   # scheduled cuff
                    if request(b, tb):
                        continue
                an = cuffs[-max_anchors:]
                age = np.array([(tb - t[win[a]]) / 60 for a, _ in an])
                w = 0.5 ** (age / half_life_min) if np.isfinite(half_life_min) else np.ones(len(an))
                w = w / w.sum()
                est = (w[:, None] * np.array([v + P[a, b] for a, v in an])).sum(axis=0)
                last = cuffs[-1][1]
                age_last = (tb - t[win[cuffs[-1][0]]]) / 60
                if kf is not None:
                    est, sig = kf.ppg(tb, est, age_last)
                    shown = int(news2_band(est[0] - score_z * sig[0]))
                else:
                    sig = np.array([np.nan, np.nan])
                    shown = int(news2_band(est[0] - fixed_margin))
                if guard is not None:          # stage 1 says "probably SBP <= 100": score at least 2
                    gx = guard_features(est, last, age_last, F[win[b]], F[win[cuffs[-1][0]]])
                    if guard["clf"].predict_proba(gx[None])[0, 1] >= guard["thr"]:
                        shown = max(shown, 2)
                last_est, last_shown = est, shown
                disp, disp_band = est, shown
                if pending_at is not None and pending_display == "hold_cuff":
                    disp, disp_band = last, int(news2_band(last[0]))
                elif pending_at is not None and pending_display == "hold_estimate" and held is not None:
                    disp, disp_band = held
                T.append(L[win[b]]); E.append(disp); H.append(last)
                S.append(sig); PB.append(disp_band); AGE.append(age_last); CID.append(c.get("subject", -1))
                POS.append((c_i, win[b], win[cuffs[-1][0]]))
                if pending_at is not None:
                    continue                   # a cuff is already on its way
                if unc_trigger and shown > news2_band(last[0]) and q[win[b]] >= cal_sqi \
                        and tb - t[win[cuffs[-1][0]]] >= min_gap_min * 60:
                    request(b, tb)
                    continue
                if trigger_mmhg is not None and tb - t[win[cuffs[-1][0]]] >= min_gap_min * 60 \
                        and q[win[b]] >= cal_sqi:
                    change = est[0] - last[0]
                    moved = (-change if drop_only else abs(change)) >= trigger_mmhg
                    # band change only counts when the estimate is band_margin mmHg inside the new band
                    nb = news2_band(est[0])
                    band = nb != news2_band(last[0]) and nb == news2_band(est[0] + band_margin) == news2_band(est[0] - band_margin)
                    if drop_only:
                        band = band and nb > news2_band(last[0])
                    if moved or band:
                        request(b, tb)
            ncuff.append(len(cuffs))
    out = (np.array(T), np.array(E), np.array(H), np.array(ncuff))
    if extra:
        return out + ({"sigma": np.array(S).reshape(-1, 2), "band": np.array(PB), "age": np.array(AGE),
                       "subject": np.array(CID), "pos": np.array(POS, dtype=int).reshape(-1, 3)},)
    return out


def guard_features(est, last, age_min, f_now, f_cuff):
    """Inputs of the stage-1 classifier (probability that SBP <= 100): the
    current estimate, the last cuff, time since it, and pulse shape now and
    its change since that cuff."""
    return np.concatenate([[est[0], est[1], last[0], last[1], age_min, est[0] - last[0]], f_now, f_now - f_cuff])


def schedule_metrics(T, E, hypo_thr=None, crit=None, PB=None):
    """PB: NEWS2 SBP band shown to the crew, when it is not simply the band of
    the estimate (cautious Kalman scoring)."""
    e = E[:, 0] - T[:, 0]
    a = np.abs(e)
    tb = news2_band(T[:, 0])
    pb = news2_band(E[:, 0]) if PB is None else np.asarray(PB)
    low = tb >= 2
    w5, w10, w15 = (float(np.mean(a <= k) * 100) for k in (5, 10, 15))
    bhs = "A" if (w5 >= 60 and w10 >= 85 and w15 >= 95) else "B" if (w5 >= 50 and w10 >= 75 and w15 >= 90) \
        else "C" if (w5 >= 40 and w10 >= 65 and w15 >= 85) else "D"
    out = {"MAE": float(a.mean()), "SD": float(e.std(ddof=1)), "w5": w5, "w10": w10, "w15": w15, "BHS": bhs,
           "DBP_MAE": float(np.abs(E[:, 1] - T[:, 1]).mean()),
           "band_exact": float(np.mean(tb == pb) * 100), "band_within1": float(np.mean(np.abs(tb - pb) <= 1) * 100),
           "miss": float(np.mean(pb[low] == 0) * 100) if low.any() else float("nan"),
           # over-triage: true score 0 shown as score >= 2
           "overcall": float(np.mean(pb[tb == 0] >= 2) * 100) if (tb == 0).any() else float("nan")}
    # Hypotension alert on this schedule's estimates: estimate <= hypo_thr, event true SBP <= 100
    hyp = T[:, 0] <= 100
    thr = 100.0 if hypo_thr is None else hypo_thr
    alert = E[:, 0] <= thr
    out["hypo_sens"] = float(np.mean(alert[hyp]) * 100) if hyp.any() else float("nan")
    out["hypo_fa"] = float(np.mean(alert[~hyp]) * 100) if (~hyp).any() else float("nan")
    if crit:
        checks = {"within1": out["band_within1"] >= crit["within1"], "miss": out["miss"] <= crit["max_miss"],
                  "hypo": out["hypo_sens"] >= crit["min_hypo_sens"]}
        if crit.get("max_overcall") is not None:   # triage criteria: over-triage limit replaces within one band
            checks["within1"] = out["overcall"] <= crit["max_overcall"]
        out["checks"] = checks
        out["news2_usable"] = all(checks.values())
    return out


def mae_sd(true, est):
    e = est[:, 0] - true[:, 0]
    return float(np.abs(e).mean()), float(e.std(ddof=1)), float(np.abs(est[:, 1] - true[:, 1]).mean())


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"))
    ap.add_argument("--cal-every-min", type=float, default=2.0)
    ap.add_argument("--max-gap-min", type=float, default=15.0)
    ap.add_argument("--max-targets", type=int, default=20, help="training targets sampled per calibration window")
    ap.add_argument("--low-bp-weight", type=float, default=3.0)
    ap.add_argument("--cal-sqi", type=float, default=0.90)
    ap.add_argument("--min-sqi", type=float, default=0.80)
    ap.add_argument("--cuff-source", choices=["art", "nibp"], default="art")
    ap.add_argument("--cuff-noise", type=float, default=6.0, help="SD of simulated cuff error, mmHg")
    ap.add_argument("--max-false-alarm", type=float, default=20.0)
    ap.add_argument("--no-rf", action="store_true", help="skip the random forest (faster, no ensemble)")
    ap.add_argument("--news2-within1", type=float, default=95.0,
                    help="NEWS2 usability: minimum %% of windows within one SBP band")
    ap.add_argument("--news2-max-miss", type=float, default=5.0,
                    help="NEWS2 usability: maximum %% of score >= 2 windows shown as score 0")
    ap.add_argument("--news2-min-hypo-sens", type=float, default=80.0,
                    help="NEWS2 usability: minimum hypotension alert sensitivity (SBP <= 100), %%")
    ap.add_argument("--news2-criteria", choices=["band", "triage"], default="band",
                    help="band: within-one-band rule; triage: over-triage limit instead (under-triage weighs more)")
    ap.add_argument("--news2-max-overtriage", type=float, default=10.0,
                    help="triage criteria: maximum %% of true score 0 windows shown as score >= 2")
    ap.add_argument("--smart-trigger", type=float, default=8.0,
                    help="extra cuff when the estimate moves this many mmHg from the last cuff")
    ap.add_argument("--no-kalman", action="store_true", help="skip section 6 (Kalman fusion)")
    ap.add_argument("--band-weights", choices=["none", "sqrt", "inverse"], default="inverse",
                    help="also train a band-balanced model; it becomes final if it cuts validation dangerous "
                         "misses (5 min cuffs) by >= 0.5 points without raising validation SBP MAE by > 0.3")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    T0 = time.time()

    cases = load_cases(Path(args.cache))
    if len(cases) < 10:
        raise SystemExit(f"only {len(cases)} usable cases in {args.cache}; run vitaldb_download.py first")
    data = split_cases(cases, args.seed)
    nwin = {k: sum(len(c["t"]) for c in v) for k, v in data.items()}
    clean = {k: sum(int((c["sqi"] >= args.min_sqi).sum()) for c in v) for k, v in data.items()}
    print({k: len(v) for k, v in data.items()}, "cases;", nwin, "windows;", clean, "clean")

    pidx = lambda cs, cl, mt: pair_index(cs, args.cal_every_min * 60, args.max_gap_min * 60, mt, rng,  # noqa: E731
                                         cl, args.cal_sqi, args.min_sqi)
    tr_all, tr_cln = pidx(data["train"], False, args.max_targets), pidx(data["train"], True, args.max_targets)
    va_idx, te_idx = pidx(data["val"], True, None), pidx(data["test"], True, None)
    print(f"pairs train all {len(tr_all)} clean {len(tr_cln)}; val {len(va_idx)} test {len(te_idx)} (clean, all targets)")

    Xva1, Yva, Cva, Gva = build(data["val"], va_idx, "v1")
    Xte1, Yte, Cte, Gte = build(data["test"], te_idx, "v1")
    Xva2 = build(data["val"], va_idx, "v2")[0]
    Xte2 = build(data["test"], te_idx, "v2")[0]

    rows_t, rows_v, preds = [], [], {}

    def ev(name, dv, dt):
        rows_v.append((name, metrics(Cva + Yva, Cva + dv, Cva, Yva, dv)))
        rows_t.append((name, metrics(Cte + Yte, Cte + dt, Cte, Yte, dt)))
        m = rows_t[-1][1]
        print(f"  {name:<52} test SBP MAE {m['SBP']['MAE']:.2f} SD {m['SBP']['SD']:.2f} r {m['SBP']['r_change']:.2f} "
              f"| DBP MAE {m['DBP']['MAE']:.2f} SD {m['DBP']['SD']:.2f}   [{time.time() - T0:.0f} s]", flush=True)

    ev("Hold last cuff value", np.zeros_like(Yva), np.zeros_like(Yte))
    # A
    X, Y, C, _ = build(data["train"], tr_all, "v1")
    mA = fit(hgb(args.seed), X, Y, C, args.low_bp_weight)
    ev("A. v1 gradient boosting, all windows", mA.predict(Xva1), mA.predict(Xte1))
    # B
    X, Y, C, _ = build(data["train"], tr_cln, "v1")
    mB = fit(hgb(args.seed), X, Y, C, args.low_bp_weight)
    ev("B. + clean windows only", mB.predict(Xva1), mB.predict(Xte1))
    # C
    X2, Y2, C2, _ = build(data["train"], tr_cln, "v2")
    mC = fit(hgb(args.seed), X2, Y2, C2, args.low_bp_weight)
    pv, pt = mC.predict(Xva2), mC.predict(Xte2)
    ev("C. + recent trend + time since cuff", pv, pt)
    # D: smoothing window chosen on validation
    best_s, best_mae = 0.0, rows_v[-1][1]["SBP"]["MAE"]
    for s in (30.0, 60.0, 90.0, 120.0):
        sv = smooth_pairs(data["val"], va_idx, pv, s)
        mae = np.abs(sv[:, 0] - Yva[:, 0]).mean()
        print(f"    smoothing {s:.0f} s: val SBP MAE {mae:.3f}")
        if mae < best_mae - 0.02:
            best_s, best_mae = s, mae
    ev(f"D. + smoothing over {best_s:.0f} s", smooth_pairs(data["val"], va_idx, pv, best_s),
       smooth_pairs(data["test"], te_idx, pt, best_s))
    final, final_name = mC, "gradient boosting"
    band_note, alt = "", None
    if args.band_weights != "none":
        t0 = time.time()
        mW = fit(hgb(args.seed), X2, Y2, C2, args.low_bp_weight, weights=band_weights(Y2, C2, args.band_weights))
        ev(f"C2. + band-balanced weights ({args.band_weights})", smooth_pairs(data["val"], va_idx, mW.predict(Xva2), best_s),
           smooth_pairs(data["test"], te_idx, mW.predict(Xte2), best_s))
        crit0 = {"within1": args.news2_within1, "max_miss": args.news2_max_miss, "min_hypo_sens": args.news2_min_hypo_sens}
        vm = {}
        for nm, mm in (("base", mC), ("band", mW)):
            Tq, Eq, _, _ = schedule_sim(data["val"], mm, 5, None, args.cuff_noise, np.random.default_rng(7), 5.0)
            vm[nm] = schedule_metrics(Tq, Eq, None, crit0)
        mae_v = {"base": rows_v[-2][1]["SBP"]["MAE"], "band": rows_v[-1][1]["SBP"]["MAE"]}
        better = vm["band"]["miss"] <= vm["base"]["miss"] - 0.5 and mae_v["band"] <= mae_v["base"] + 0.3
        band_note = (f"Band-balanced weights ({args.band_weights}): validation dangerous miss at 5 min "
                     f"{vm['base']['miss']:.1f}% -> {vm['band']['miss']:.1f}%, validation SBP MAE "
                     f"{mae_v['base']:.2f} -> {mae_v['band']:.2f}; "
                     + ("**used as the final model**." if better else "not used (rule: >= 0.5 points fewer misses, "
                                                                   "MAE no more than 0.3 worse)."))
        print("  " + band_note.replace("**", "") + f"  [{time.time() - t0:.0f} s]", flush=True)
        if better:
            final, final_name, alt = mW, f"gradient boosting, band-balanced weights ({args.band_weights})", ("unweighted", mC)
            pv, pt = mW.predict(Xva2), mW.predict(Xte2)
        else:
            alt = (f"band-balanced ({args.band_weights})", mW)
    # E
    if not args.no_rf:
        t0 = time.time()
        mR = fit(rf(args.seed), X2, Y2, C2, args.low_bp_weight)
        print(f"    random forest fit {time.time() - t0:.0f} s")
        ens = Ensemble([mC, mR])
        ev_v, ev_t = ens.predict(Xva2), ens.predict(Xte2)
        ev("E. + ensemble (gradient boosting + random forest)", smooth_pairs(data["val"], va_idx, ev_v, best_s),
           smooth_pairs(data["test"], te_idx, ev_t, best_s))
        if rows_v[-1][1]["SBP"]["MAE"] < rows_v[-2][1]["SBP"]["MAE"]:
            final, final_name = ens, "ensemble (gradient boosting + random forest)"
            pt = ev_t
    print("final model:", final_name)
    del X, Y, C, X2, Y2, C2

    # ---- 2. error against minutes since cuff (final, smoothed)
    pt_s = smooth_pairs(data["test"], te_idx, pt, best_s)
    drift = []
    for lo, hi in [(0.25, 2), (2, 5), (5, 10), (10, 15)]:
        m = (Gte > lo * 60) & (Gte <= hi * 60)
        if m.sum() < 10:
            continue
        drift.append((f"{lo:g}-{hi:g} min", int(m.sum()), np.abs(Yte[m, 0]).mean(),
                      np.abs(Yte[m, 0] - pt_s[m, 0]).mean(), float(np.mean(np.abs(Yte[m, 0]) >= 20) * 100)))

    # ---- 3. half-life on validation, then cuff-gap experiment on test
    print("simulating the cuff protocol (a few minutes)...", flush=True)
    hl_scores = {}
    for hl in (2.5, 5.0, 10.0, np.inf):
        s = simulate(data["val"], final, "v2", 5, args.cuff_noise, args.cuff_source, np.random.default_rng(5),
                     hl, best_s)
        if len(s["true"]):
            hl_scores[hl] = mae_sd(s["true"], s["model_multi"])[0]
    half_life = min(hl_scores, key=hl_scores.get) if hl_scores else np.inf
    print("cuff half-life (val SBP MAE):", {str(k): round(v, 3) for k, v in hl_scores.items()}, "->", half_life)

    gap_rows = []
    for noise in sorted({0.0, args.cuff_noise}):
        for gap in (None, 2, 5, 10):
            res = {}
            sim = simulate(data["test"], final, "v2", gap, noise, args.cuff_source, np.random.default_rng(1), half_life, best_s)
            sim_eq = simulate(data["test"], final, "v2", gap, noise, args.cuff_source, np.random.default_rng(1), np.inf, best_s)
            if len(sim["true"]) < 10:
                continue
            for k in ("hold", "hold_mean", "model_latest"):
                res[k] = mae_sd(sim["true"], sim[k])
            res["model_equal"] = mae_sd(sim_eq["true"], sim_eq["model_multi"])
            res["model_weighted"] = mae_sd(sim["true"], sim["model_multi"])
            gap_rows.append((noise, gap, len(sim["true"]), res))
            print(f"  noise {noise:g} gap {gap}: " + "  ".join(f"{k} {v[0]:.2f}" for k, v in res.items()), flush=True)
        if args.cuff_source == "nibp":
            break

    # ---- 4. alerts (cuffs at 0 and 5 min)
    sim_v = simulate(data["val"], final, "v2", 5, args.cuff_noise, args.cuff_source, np.random.default_rng(2), half_life, best_s)
    sim_t = simulate(data["test"], final, "v2", 5, args.cuff_noise, args.cuff_source, np.random.default_rng(3), half_life, best_s)
    alerts, settings = [], {}
    for label, ev_fn, sc_fn, key in (
        ("SBP drop >= 20 mmHg vs last cuff", lambda s: (s["true"] - s["latest"])[:, 0] <= -20,
         lambda s: -(s["model_multi"] - s["latest"])[:, 0], "drop_alert_mmhg"),
        ("SBP <= 100", lambda s: s["true"][:, 0] <= 100, lambda s: -s["model_multi"][:, 0], "hypotension_alert_sbp"),
        ("SBP <= 90", lambda s: s["true"][:, 0] <= 90, lambda s: -s["model_multi"][:, 0], None),
    ):
        if len(sim_v["true"]) < 20 or len(sim_t["true"]) < 20:
            break
        evv, scv, evt, sct = ev_fn(sim_v), sc_fn(sim_v), ev_fn(sim_t), sc_fn(sim_t)
        if evv.sum() < 5 or evt.sum() < 5:
            continue
        thr = threshold_for_false_alarm(evv, scv, args.max_false_alarm)
        a = alert_curve(evt, sct, [thr])[0]
        shown = f"predicted drop >= {thr:.1f}" if "drop" in label else f"estimate <= {-thr:.1f}"
        ref = ""
        if "drop" not in label:
            cut = 100 if "100" in label else 90
            h = alert_curve(evt, -sim_t["hold"][:, 0], [-cut])[0]
            ref = f"last cuff <= {cut}: sens {h['sens']:.1f}%, FA {h['false_alarm']:.1f}%"
        alerts.append((label, auroc(evt, sct), shown, a, int(evt.sum()), ref))
        if key:
            settings[key] = round(thr if "drop" in label else -thr, 1)

    # ---- 5. cuff schedules: fixed intervals vs 10 min + smart extra cuffs
    print("simulating cuff schedules...", flush=True)
    sched_rows = []
    crit = {"within1": args.news2_within1, "max_miss": args.news2_max_miss, "min_hypo_sens": args.news2_min_hypo_sens,
            "max_overcall": args.news2_max_overtriage if args.news2_criteria == "triage" else None}
    first_rule = (f"at least {args.news2_within1:g}% of windows within one SBP band" if args.news2_criteria == "band"
                  else f"at most {args.news2_max_overtriage:g}% over-triage (true score 0 shown as >= 2)")
    hypo_thr = settings.get("hypotension_alert_sbp", 100.0)
    sched_list = [("Fixed every 15 min", 15, None, False), ("Fixed every 10 min", 10, None, False),
                  ("Fixed every 5 min", 5, None, False), ("Fixed every 3 min", 3, None, False),
                  (f"10 min + extra cuff on any {args.smart_trigger:g} mmHg change or NEWS2 band change",
                   10, args.smart_trigger, False),
                  (f"10 min + extra cuff on {args.smart_trigger:g} mmHg drop or worse NEWS2 band",
                   10, args.smart_trigger, True)]
    if alt:                                    # same fixed schedules with the model that was not chosen
        sched_list += [((f"Fixed every 5 min, {alt[0]} model", alt[1]), 5, None, False),
                       ((f"Fixed every 10 min, {alt[0]} model", alt[1]), 10, None, False)]
    for label, base, trig, drop in sched_list:
        mdl = final
        if isinstance(label, tuple):           # comparison row with the other model
            label, mdl = label
        Ts, Es, Hs, nc = schedule_sim(data["test"], mdl, base, trig, args.cuff_noise, np.random.default_rng(11),
                                      half_life if np.isfinite(half_life) else np.inf, drop_only=drop)
        if len(Ts) < 10:
            continue
        sched_rows.append((label, float(nc.mean()), schedule_metrics(Ts, Es, hypo_thr, crit),
                           schedule_metrics(Ts, Hs, 100.0, crit)))
        m = sched_rows[-1][2]
        print(f"  {label:<62} cuffs/15 min {nc.mean():.2f}  SBP MAE {m['MAE']:.2f}  BHS {m['BHS']}  "
              f"within 1 band {m['band_within1']:.1f}%  dangerous miss {m['miss']:.1f}%  "
              f"hypotension alert {m['hypo_sens']:.1f}%  NEWS2 usable {'YES' if m['news2_usable'] else 'no'}", flush=True)

    # ---- 6. Kalman fusion + cautious NEWS2 score (tuned on validation, reported on test)
    kal_rows, kcfg, kz = [], None, None
    if not args.no_kalman:
        print("Kalman fusion: fitting error growth and tuning on validation...", flush=True)
        hl = half_life if np.isfinite(half_life) else np.inf
        Tv, Ev, _, _, xv = schedule_sim(data["val"], final, 15, None, args.cuff_noise, np.random.default_rng(3), hl, extra=True)
        s0, s1 = fit_error_growth(xv["age"], (Ev - Tv)[:, 0])
        d0, _ = fit_error_growth(xv["age"], (Ev - Tv)[:, 1])
        best = None
        for qk in (8.0, 32.0, 128.0):
            for rk in (0.5, 1.0):
                c_ = KalmanConfig(q=qk, cuff_sd=max(args.cuff_noise, 1.0), s0=s0, s1=s1, dbp_ratio=d0 / s0, r_scale=rk)
                Tq, Eq, _, _ = schedule_sim(data["val"], final, 10, None, args.cuff_noise, np.random.default_rng(5), hl, kalman=c_)
                mae = float(np.abs(Eq[:, 0] - Tq[:, 0]).mean())
                if best is None or mae < best[0]:
                    best = (mae, c_)
        kcfg = best[1]
        print(f"  model error SD {s0:.1f} + {s1:.2f} x minutes since cuff; q {kcfg.q:g}, r_scale {kcfg.r_scale:g} "
              f"(val SBP MAE {best[0]:.2f})  [{time.time() - T0:.0f} s]", flush=True)
        # cautious score z chosen on validation (5 min schedule): passes the NEWS2 check with the smallest z,
        # otherwise the z with the fewest dangerous misses that still keeps the within-one-band criterion
        zs = {}
        for zk in (0.0, 0.3, 0.5, 0.7, 1.0):
            Tz, Ez, _, _, xz = schedule_sim(data["val"], final, 5, None, args.cuff_noise, np.random.default_rng(7), hl,
                                            kalman=kcfg, score_z=zk, extra=True)
            zs[zk] = schedule_metrics(Tz, Ez, hypo_thr, crit, PB=xz["band"])
            print(f"    val z {zk:g}: within 1 band {zs[zk]['band_within1']:.1f}%  dangerous miss {zs[zk]['miss']:.1f}%  "
                  f"over-triage {zs[zk]['overcall']:.1f}%", flush=True)
        ok = [zk for zk, r in zs.items() if r["news2_usable"]]
        # "keep" = z values that pass the first rule (within one band, or the over-triage limit)
        keep = [zk for zk, r in zs.items() if r["checks"]["within1"]]
        kz = min(ok) if ok else (min(keep, key=lambda zk: zs[zk]["miss"]) if keep else 0.5)
        print(f"  cautious score z = {kz:g}", flush=True)
        for label, base, kw in (
                ("Fixed every 10 min, current estimate", 10, {}),
                ("Fixed every 10 min, Kalman", 10, {"kalman": kcfg}),
                (f"Fixed every 10 min, Kalman, cautious score z={kz:g}", 10, {"kalman": kcfg, "score_z": kz}),
                (f"10 min + uncertainty cuff, Kalman z={kz:g}", 10, {"kalman": kcfg, "score_z": kz, "unc_trigger": True}),
                ("Fixed every 5 min, current estimate", 5, {}),
                ("Fixed every 5 min, score 3 mmHg lower (no Kalman)", 5, {"fixed_margin": 3.0}),
                ("Fixed every 5 min, Kalman", 5, {"kalman": kcfg}),
                (f"Fixed every 5 min, Kalman, cautious score z={kz:g}", 5, {"kalman": kcfg, "score_z": kz}),
                (f"5 min + uncertainty cuff, Kalman z={kz:g}", 5, {"kalman": kcfg, "score_z": kz, "unc_trigger": True}),
                (f"Fixed every 3 min, Kalman, cautious score z={kz:g}", 3, {"kalman": kcfg, "score_z": kz})):
            Tk, Ek, _, nck, xk = schedule_sim(data["test"], final, base, None, args.cuff_noise, np.random.default_rng(11),
                                              hl, extra=True, **kw)
            if len(Tk) < 10:
                continue
            mk = schedule_metrics(Tk, Ek, hypo_thr, crit, PB=xk["band"])
            sg = xk["sigma"][:, 0]
            cov = float(np.mean(np.abs(Ek[:, 0] - Tk[:, 0]) <= 1.96 * sg) * 100) if np.isfinite(sg).all() else float("nan")
            kal_rows.append((label, float(nck.mean()), mk, cov))
            print(f"  {label:<55} cuffs/15 min {nck.mean():.2f}  SBP MAE {mk['MAE']:.2f}  within 1 band "
                  f"{mk['band_within1']:.1f}%  dangerous miss {mk['miss']:.1f}%  over-triage {mk['overcall']:.1f}%  "
                  f"NEWS2 usable {'YES' if mk['news2_usable'] else 'no'}", flush=True)
        settings["kalman"] = kcfg.to_dict()
        settings["news2_score_z"] = kz

    # ---- save
    cfg = CalibrationConfig()
    bundle = {"model": final, "model_name": final_name, "feature_version": FEATURE_VERSION, "layout": "v2",
              "n_inputs": 3 * N_FEATURES + 1, "uses_cal_bp": False, "smooth_s": best_s,
              "anchor_half_life_min": None if not np.isfinite(half_life) else half_life,
              "min_estimate_sqi": args.min_sqi, "config": asdict(cfg), "trained_on": "VitalDB",
              "test_metrics": rows_t[-1][1]}
    joblib.dump(bundle, ROOT / "model" / "bp_calibrated_vitaldb.joblib")
    settings.update({"gain_lambda": None, "max_anchors": 3, "max_false_alarm_pct": args.max_false_alarm,
                     "trained_on": "VitalDB"})
    (ROOT / "model" / "alert_settings_vitaldb.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")

    hl_txt = "equal weights" if not np.isfinite(half_life) else f"half-life {half_life:g} min"
    md = [f"# VitalDB results (v2)\n\nCases: train {len(data['train'])}, val {len(data['val'])}, test {len(data['test'])} "
          f"(split by patient). Windows {nwin}, clean (SQI >= {args.min_sqi}) {clean}. Test pairs: {len(te_idx)} "
          f"(clean, 15 s to {args.max_gap_min:g} min after a cuff). Final: **{final_name}**, smoothing {best_s:.0f} s, "
          f"cuff averaging {hl_txt}.\n\n" + (band_note + "\n\n" if band_note else ""),
          "## 1. What each improvement adds (test pairs, single cuff)\n\n", table(rows_t),
          "\n## 2. SBP error against minutes since the cuff (test, final pipeline)\n\n",
          "| Time since cuff | Pairs | Hold cuff MAE | Model MAE | True change >= 20 mmHg |\n|---|---|---|---|---|\n"]
    md += [f"| {b_} | {n} | {h:.2f} | {m:.2f} | {p:.1f}% |\n" for b_, n, h, m, p in drift]
    md.append(f"\n## 3. When to take the second cuff (test; cuff at 0 min, second at the gap, estimates to 15 min)\n\n"
              f"Cuff source: {args.cuff_source}. SBP MAE (SD) / DBP MAE. Cuff averaging chosen on validation: {hl_txt} "
              f"(validation SBP MAE by half-life: {', '.join(f'{k:g} min {v:.2f}' for k, v in hl_scores.items())}).\n\n"
              "| Cuff error SD | Second cuff | Windows | Hold last cuff | Hold mean of cuffs | Model, last cuff | "
              "Model, cuffs averaged equally | Model, cuffs time-weighted |\n|---|---|---|---|---|---|---|---|\n")
    for noise, gap, n, res in gap_rows:
        cells = " | ".join(f"{res[k][0]:.2f} ({res[k][1]:.1f}) / {res[k][2]:.2f}"
                           for k in ("hold", "hold_mean", "model_latest", "model_equal", "model_weighted"))
        err = "real NIBP" if args.cuff_source == "nibp" else f"{noise:g}"
        md.append(f"| {err} | {'none' if gap is None else f'{gap} min'} | {n} | {cells} |\n")
    md.append(f"\n## 4. Alerts (cuffs at 0 and 5 min; thresholds tuned on validation for <= {args.max_false_alarm:g}% false alarms)\n\n"
              "| Alert | Events (test) | AUROC | Threshold | Sensitivity | False alarm | PPV | Cuff-only rule |\n|---|---|---|---|---|---|---|---|\n")
    for label, auc, shown, a, n_ev, ref in alerts:
        md.append(f"| {label} | {n_ev} | {auc:.2f} | {shown} | {a['sens']:.1f}% | {a['false_alarm']:.1f}% | {a['ppv']:.1f}% | {ref} |\n")
    md.append(f"\n## 5. Cuff schedules and NEWS2 (test; cuff error SD {args.cuff_noise:g}; estimates over 15 min; "
              "no output smoothing)\n\n"
              "Dangerous miss = true NEWS2 SBP score >= 2 shown as 0. BHS grades: A >= 60/85/95%, B >= 50/75/90%, "
              "C >= 40/65/85% within 5/10/15 mmHg.\n\n"
              "| Schedule | Cuffs per 15 min | SBP MAE (SD) | Within 5/10/15 mmHg | BHS | DBP MAE | NEWS2 band exact "
              "| Within one band | Dangerous miss | Hold-cuff MAE / miss |\n|---|---|---|---|---|---|---|---|---|---|\n")
    for label, ncm, m, h in sched_rows:
        md.append(f"| {label} | {ncm:.1f} | {m['MAE']:.2f} ({m['SD']:.1f}) | {m['w5']:.0f} / {m['w10']:.0f} / {m['w15']:.0f}% "
                  f"| {m['BHS']} | {m['DBP_MAE']:.2f} | {m['band_exact']:.1f}% | {m['band_within1']:.1f}% | {m['miss']:.1f}% "
                  f"| {h['MAE']:.2f} / {h['miss']:.1f}% |\n")
    tick = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
    tri = args.news2_criteria == "triage"
    t_w1 = lambda c: "" if tri else " " + tick(c["within1"])  # noqa: E731
    t_ov = lambda c: " " + tick(c["within1"]) if tri else ""  # noqa: E731
    md.append(f"\n### NEWS2 usability check (proposed SIREN criteria, not an official standard)\n\n"
              f"Criteria: {args.news2_criteria}. Usable for NEWS2 when all three hold: {first_rule}, "
              f"at most {args.news2_max_miss:g}% dangerous misses, and hypotension alert sensitivity (SBP <= 100, alert at "
              f"estimate <= {hypo_thr:g} mmHg) of at least {args.news2_min_hypo_sens:g}%.\n\n"
              "| Schedule | Within one band | Dangerous miss | Hypotension alert sens / false alarm | Over-triage | NEWS2 usable | "
              "Hold cuff (alert at cuff <= 100) |\n|---|---|---|---|---|---|---|\n")
    for label, ncm, m, h in sched_rows:
        c = m["checks"]
        md.append(f"| {label} | {m['band_within1']:.1f}%{t_w1(c)} | {m['miss']:.1f}% {tick(c['miss'])} "
                  f"| {m['hypo_sens']:.1f}% / {m['hypo_fa']:.1f}% {tick(c['hypo'])} | {m['overcall']:.1f}%{t_ov(c)} "
                  f"| **{'YES' if m['news2_usable'] else 'NO'}** "
                  f"| {'YES' if h['news2_usable'] else 'NO'} ({h['band_within1']:.0f}% / {h['miss']:.1f}% / {h['hypo_sens']:.0f}%) |\n")
    md.append("\nOver-triage = true NEWS2 SBP score 0 shown as score >= 2"
              + (" (part of the check).\n" if tri else " (reported, not part of the check).\n"))
    if kal_rows:
        md.append(f"\n## 6. Kalman fusion and cautious NEWS2 score (test; tuned on validation)\n\n"
                  f"The filter fuses cuffs (error SD {kcfg.cuff_sd:g}) with the PPG estimate, whose error SD is modelled as "
                  f"{kcfg.s0:.1f} + {kcfg.s1:.2f} x minutes since the cuff (fitted on validation); q {kcfg.q:g} mmHg^2/min, "
                  f"r_scale {kcfg.r_scale:g}. Cautious score = NEWS2 band of (estimate - {kz:g} x sigma), z chosen on "
                  "validation. Uncertainty cuff = extra cuff when the cautious score is worse than the last cuff's "
                  "(at most one per 2 min). 95% range coverage should be near 95% if sigma is honest.\n\n"
                  "| Schedule | Cuffs per 15 min | SBP MAE | Within one band | Dangerous miss | Over-triage | "
                  "Hypotension alert sens | 95% range coverage | NEWS2 usable |\n|---|---|---|---|---|---|---|---|---|\n")
        for label, ncm, m, cov in kal_rows:
            c = m["checks"]
            covs = "n/a" if not np.isfinite(cov) else f"{cov:.0f}%"
            md.append(f"| {label} | {ncm:.1f} | {m['MAE']:.2f} | {m['band_within1']:.1f}%{t_w1(c)} "
                      f"| {m['miss']:.1f}% {tick(c['miss'])} | {m['overcall']:.1f}%{t_ov(c)} | {m['hypo_sens']:.1f}% {tick(c['hypo'])} "
                      f"| {covs} | **{'YES' if m['news2_usable'] else 'NO'}** |\n")
    md.append("\nVitalDB patients are under general anaesthesia, so BP changes are mostly drug-driven "
              "(induction, vasopressors), not trauma; the PPG comes from a bedside oximeter, not the MAX30102.\n")
    (out / "vitaldb_comparison.md").write_text("".join(md), encoding="utf-8")
    print("".join(md))
    print(f"written {out / 'vitaldb_comparison.md'}, model/bp_calibrated_vitaldb.joblib, "
          f"model/alert_settings_vitaldb.json  [{time.time() - T0:.0f} s]")


if __name__ == "__main__":
    main()
