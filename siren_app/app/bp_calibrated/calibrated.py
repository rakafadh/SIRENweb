"""Cuff-calibrated BP trend model: the version SIREN should run.

How it works
  1. The crew takes a cuff reading. The 30 s PPG window recorded at the same
     time becomes the calibration window.
  2. For every new window, the model predicts the CHANGE in SBP and DBP since
     calibration from (new features, new minus calibration features, cuff BP).
  3. Estimate = cuff value + predicted change.
  4. Status flags say when a new cuff reading is due (see CalibrationConfig).

Pair layout (training and inference must match)
  x = [f_now (26), f_now - f_cal (26), sbp_cal, dbp_cal]   -> 54 numbers
  y = [sbp_now - sbp_cal, dbp_now - dbp_cal]
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import joblib
import numpy as np

from .features_v2 import FEATURE_VERSION, FS, N_FEATURES, extract_sensor
from .kalman import BpKalman, KalmanConfig
from .multipoint import combine, fit_gain, pair_arrays
from .quality import QualityGate, ok_calibrate, ok_estimate

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"


@dataclass
class CalibrationConfig:
    """Calibration rules. Values come from the literature discussed in the
    README (Sun et al.; Tae et al. 2026; ESH 2023) and are meant to be tuned
    on SIREN's own volunteer data."""
    window_s: float = 30.0                 # PPG window paired with each cuff reading
    min_sqi: float = 0.90                  # template correlation needed to accept a calibration window
    min_beats: int = 15                    # beats needed in the calibration window
    max_motion_g: float = 0.05             # accelerometer RMS above this: do not calibrate
    max_interval_unstable_min: float = 5   # NEWS2 >= 5 or any red score (VitalDB: 5 min gives BHS ~B)
    max_interval_stable_min: float = 10
    smart_trigger_mmhg: float = 8          # extra cuff when the estimate moves this far from the last cuff
    smart_drop_only: bool = False          # True: only drops and worse NEWS2 bands trigger (fewer cuffs)
    min_cuff_gap_min: float = 2            # never ask for cuffs closer together than this
    # Confirmatory cuff: off by default. Robustness check (728 cases): +0.2-0.35 cuffs per 15 min for at most
    # 0.4 points fewer misses with the 8 mmHg trigger, none with the 5 mmHg trigger (4.0 -> 4.2%).
    confirm_mmhg: float = 0                # > 0: cuff differs from the estimate just before it by more: confirm
    confirm_after_min: float = 2           # ... with one more cuff this long after
    # While a requested cuff is pending (cuff_now until calibrate() arrives) the dashboard shows:
    #   "hold_estimate" = the estimate and NEWS2 score from the moment the cuff was asked for (default)
    #   "live"          = keep updating the PPG estimate
    #   "hold_cuff"     = the last cuff value
    # Drop / hypotension / rise / HR alerts always use the live estimate. Simulation (40 test patients,
    # cuff SD 6): dangerous miss live 4.5 / 4.7 / 4.9%, hold_estimate 4.8 / 5.2 / 5.9%, hold_cuff
    # 6.2 / 7.3 / 10.5% for a 0.75 / 1.5 / 3 min wait; holding costs little only when the cuff comes fast.
    pending_display: str = "hold_estimate"
    cuff_overdue_min: float = 5            # CUFF_OVERDUE reason when a requested cuff is this late
    stale_after_min: float = 20            # estimate hidden after this long without a cuff
    trend_alert_mmhg: float = 20           # |predicted change| that triggers a cuff check
    recal_on_band_change: bool = True      # predicted SBP crosses a NEWS2 SBP band
    recal_on_finger_lost: bool = True      # sensor repositioned: old baseline no longer fits
    hr_change_frac: float = 0.25           # HR moved this much since calibration: cuff check
    min_estimate_sqi: float = 0.80         # windows below this give no estimate
    # Rescue gate (bp_model/quality.py): also accept a window with a lower score when most of its
    # beats are clean (a twitch spoilt a few beats). Off unless the settings file's "quality"
    # block, written by scripts/quality_gate_check.py --write after the VitalDB check, turns it on.
    quality_rescue: bool = False
    rescue_est_sqi: float = 0.60
    rescue_est_frac: float = 0.75
    rescue_cal_sqi: float = 0.75
    rescue_cal_frac: float = 0.85
    # Kalman mode (used when the settings file has "kalman", written by vitaldb_train.py section 6).
    # Cuff every max_interval_kalman_min plus an extra cuff whenever the cautious NEWS2 SBP score is
    # worse than the last cuff's (UNCERTAINTY), at most one per min_cuff_gap_min. Robustness check on
    # VitalDB (106 test patients, 5 cuff-error repeats): 3 min + Kalman z 0.3 dangerous miss 3.9%
    # (95% CI 2.5-5.8), the best schedule; 5 min + uncertainty cuff 5.4% (3.5-7.9), not usable.
    use_kalman: bool = True                # False: old smart triggers even when Kalman settings exist
    max_interval_kalman_min: float = 3


def pair_features(f_now: np.ndarray, f_cal: np.ndarray, sbp_cal: float, dbp_cal: float) -> np.ndarray:
    return np.concatenate([f_now, f_now - f_cal, [sbp_cal, dbp_cal]])


def pair_features_v2(f_now: np.ndarray, f_cal: np.ndarray, f_recent: np.ndarray, minutes_since_cuff: float) -> np.ndarray:
    """Layout v2 (79 numbers): pulse shape now, change since the cuff, change
    over the last 30-120 s (trend direction), and minutes since the cuff."""
    return np.concatenate([f_now, f_now - f_cal, f_now - f_recent, [minutes_since_cuff]])


N_INPUTS_V2 = 3 * N_FEATURES + 1
RECENT_FROM_S, RECENT_TO_S = 120.0, 30.0   # f_recent = mean of clean windows ending 30-120 s ago


class Ensemble:
    """Mean of several fitted regressors. Lives here (not in a script) so a
    saved model can be loaded from anywhere."""
    def __init__(self, models):
        self.models = models

    def predict(self, X):
        return np.mean([m.predict(X) for m in self.models], axis=0)

    def set_jobs(self, n):
        for m in self.models:
            est = m.steps[-1][1] if hasattr(m, "steps") else m
            if hasattr(est, "n_jobs"):
                est.n_jobs = n


PAIR_FEATURE_NAMES_SUFFIX = ["sbp_cal", "dbp_cal"]


def news2_sbp_score(sbp: float) -> int:
    if sbp <= 90:
        return 3
    if sbp <= 100:
        return 2
    if sbp <= 110:
        return 1
    if sbp <= 219:
        return 0
    return 3


def resample_25hz(times_ms: np.ndarray, values: np.ndarray, window_s: float = 30.0):
    """Newest window_s seconds of sensor IR on a uniform 25 Hz grid."""
    t = np.asarray(times_ms, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    if t.size < 2 or (t[-1] - t[0]) / 1000 < window_s - 2:
        return None
    if np.max(np.diff(t)) > 500:
        return None
    grid = np.arange(t[-1] - window_s * 1000, t[-1], 1000 / FS)
    return np.interp(grid, t, v)


@dataclass
class Calibration:
    sbp: float
    dbp: float
    t_ms: float
    features: np.ndarray = field(repr=False)


class CalibratedBpModel:
    """Cuff-calibrated estimate with multi-point calibration and tuned alerts.

    Every accepted cuff reading is kept (the newest max_anchors). With one
    cuff it behaves as single-point calibration; from the second cuff on,
    estimates average over all stored cuffs and a patient gain is fitted
    (bp_model/multipoint.py). Alert thresholds come from
    model/alert_settings.json (written by scripts/eval_multipoint.py) when it
    exists, otherwise from CalibrationConfig.
    """

    def __init__(self, path: str | Path = MODEL_DIR / "bp_calibrated.joblib",
                 alert_path: str | Path = MODEL_DIR / "alert_settings.json", schedule: str | None = None):
        """schedule (or "schedule" in the settings file):
          "smart_kalman" (default) = Kalman fusion of cuffs and PPG, cuff every 10 min (5 min when NEWS2 >= 5)
              plus an extra cuff whenever the fused estimate moves 5 mmHg from the last cuff or changes NEWS2
              SBP band; NEWS2 SBP score from estimate - 0.3 x sigma. Robustness check, 728 VitalDB cases,
              106 test patients: dangerous miss 3.3% (95% CI 2.3-4.7), within one band 96.3%, hypotension
              alert 90.4%, 4.2 cuffs per 15 min: ROBUST. Needs "kalman" in the settings file, else "smart".
          "smart" = the same triggers without Kalman: 4.0% (2.9-5.6), 4.7 cuffs per 15 min, BORDERLINE.
          "kalman" = cuff every 3 min plus UNCERTAINTY cuffs: 4.4% (3.0-6.3), 5.1 cuffs, BORDERLINE.
        "smart_trigger_mmhg" and "news2_score_z_smart" in the settings file override 5 and 0.3."""
        bundle = joblib.load(path)
        if bundle["feature_version"] != FEATURE_VERSION:
            raise ValueError(f"model expects {bundle['feature_version']}, code has {FEATURE_VERSION}")
        self.model = bundle["model"]
        if hasattr(self.model, "set_jobs"):
            self.model.set_jobs(1)
        elif hasattr(self.model, "n_jobs"):
            self.model.n_jobs = 1
        self.n_inputs = bundle.get("n_inputs", 2 * N_FEATURES + 2)
        self.layout = bundle.get("layout", "v1")          # v2: + recent trend + minutes since cuff
        self.smooth_s = float(bundle.get("smooth_s") or 0.0)
        self.half_life = bundle.get("anchor_half_life_min")  # None = cuffs averaged equally
        self.name = bundle.get("model_name")
        self.config = CalibrationConfig(**bundle.get("config", {}))
        self.metrics = bundle.get("test_metrics")
        self.alerts = {}
        if Path(alert_path).exists():
            self.alerts = json.loads(Path(alert_path).read_text(encoding="utf-8"))
        for k, v in (self.alerts.get("quality") or {}).items():     # validated quality gate, if any
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        self.cuffs: list[Calibration] = []
        self.gain = (1.0, 1.0)
        self.hist: list[tuple[float, np.ndarray]] = []        # (t_ms, features) of clean windows
        self.pred_hist: dict[float, list] = {}                # cuff t_ms -> [(t_ms, change)]
        self.kf = None
        self.score_z = 0.0
        self.last_est_sbp = None       # last SBP estimate shown, compared with the next cuff
        self.confirm_due_ms = None     # time of a pending confirmatory cuff
        self.pending_since_ms = None   # when the current cuff request started (None = no request open)
        self.held = None               # (sbp, dbp, news2 score, sigma) shown while the request is open
        self.schedule = schedule or self.alerts.get("schedule", "smart_kalman")
        if self.schedule not in ("smart_kalman", "smart", "kalman"):
            raise ValueError("schedule must be 'smart_kalman', 'smart' or 'kalman'")
        has_kf = self.config.use_kalman and "kalman" in self.alerts
        if self.schedule != "smart" and not has_kf:
            self.schedule = "smart"                    # no Kalman settings: same triggers without fusion
        # validated trigger for both smart schedules (robustness check, 22-23 Nov): 5 mmHg
        self.trigger_mmhg = float(self.alerts.get("smart_trigger_mmhg", 5.0))
        if self.schedule != "smart":
            self.kf = BpKalman(KalmanConfig(**self.alerts["kalman"]))
            self.score_z = float(self.alerts.get("news2_score_z", 0.0)) if self.schedule == "kalman" \
                else float(self.alerts.get("news2_score_z_smart", 0.3))

    @property
    def gate(self) -> QualityGate:
        c = self.config
        return QualityGate(est_sqi=c.min_estimate_sqi, cal_sqi=c.min_sqi, rescue=bool(c.quality_rescue),
                           rescue_est_sqi=c.rescue_est_sqi, rescue_est_frac=c.rescue_est_frac,
                           rescue_cal_sqi=c.rescue_cal_sqi, rescue_cal_frac=c.rescue_cal_frac, window_s=c.window_s)

    def _remember(self, t_ms: float, f: np.ndarray):
        if np.isfinite(f[-2]) and bool(ok_estimate(f, self.gate)):
            self.hist.append((t_ms, f))
        self.hist = [(t, x) for t, x in self.hist if t >= t_ms - (RECENT_FROM_S + 60) * 1000]

    def _recent(self, t_ms: float) -> np.ndarray:
        sel = [x for t, x in self.hist if t_ms - RECENT_FROM_S * 1000 <= t <= t_ms - RECENT_TO_S * 1000]
        return np.nanmean(sel, axis=0) if sel else np.full(N_FEATURES, np.nan)

    @property
    def cal(self) -> Calibration | None:
        """Latest cuff reading."""
        return self.cuffs[-1] if self.cuffs else None

    def _predict_change(self, f_now: np.ndarray, cuffs: list[Calibration], t_ms: float | None = None) -> np.ndarray:
        if self.layout == "v2":
            rec = self._recent(t_ms if t_ms is not None else cuffs[-1].t_ms)
            X = np.array([pair_features_v2(f_now, c.features, rec, max((t_ms or c.t_ms) - c.t_ms, 0) / 60000)
                          for c in cuffs])
        else:
            X = np.array([pair_features(f_now, c.features, c.sbp, c.dbp)[:self.n_inputs] for c in cuffs])
        return self.model.predict(X)   # (n_cuffs, 2): change in SBP, DBP from each cuff

    def _refit_gain(self):
        lam = self.alerts.get("gain_lambda")
        n = len(self.cuffs)
        if n < 2 or lam is None or self.layout == "v2":
            self.gain = (1.0, 1.0)
            return
        P = np.array([self._predict_change(c.features, self.cuffs) for c in self.cuffs])  # P[k, i] = change i -> k
        g = []
        for b, key in enumerate(("sbp", "dbp")):
            vals = np.array([getattr(c, key) for c in self.cuffs])
            d, y = pair_arrays(vals, P[:, :, b].T)
            g.append(fit_gain(d, y, float(lam)))
        self.gain = tuple(g)

    # ------------------------------------------------------------ calibrate
    def calibrate(self, times_ms, raw_ir, sbp: float, dbp: float, motion_g: float | None = None):
        """Call for EVERY cuff reading, the first and later ones. Returns (ok, reason)."""
        c = self.config
        if motion_g is not None and motion_g > c.max_motion_g:
            return False, "MOTION"
        w = resample_25hz(times_ms, raw_ir, c.window_s)
        if w is None:
            return False, "NOT_ENOUGH_SIGNAL"
        f = extract_sensor(w)
        if np.isnan(f).all():
            return False, "NO_BEATS"
        sqi = f[-2]
        nb = f[-3]
        if not np.isfinite(sqi) or not bool(ok_calibrate(f, self.gate)) or nb < c.min_beats:
            return False, "POOR_SIGNAL"
        t_cuff = float(times_ms[-1])
        is_confirm = self.confirm_due_ms is not None and t_cuff >= self.confirm_due_ms - 30000
        if is_confirm or not (c.confirm_mmhg and c.confirm_after_min) or self.last_est_sbp is None:
            self.confirm_due_ms = None
        elif abs(float(sbp) - self.last_est_sbp) > c.confirm_mmhg:
            self.confirm_due_ms = t_cuff + c.confirm_after_min * 60000     # cuff error or sudden change
        else:
            self.confirm_due_ms = None
        self.cuffs.append(Calibration(float(sbp), float(dbp), float(times_ms[-1]), f))
        self.pending_since_ms = self.held = None          # the requested cuff has arrived
        if self.kf is not None:
            self.kf.cuff(float(times_ms[-1]) / 1000, [float(sbp), float(dbp)])
        self.cuffs = self.cuffs[-int(self.alerts.get("max_anchors", 3)):]
        self.pred_hist = {k: v for k, v in self.pred_hist.items() if any(c.t_ms == k for c in self.cuffs)}
        self._remember(float(times_ms[-1]), f)
        self._refit_gain()
        return True, "OK"

    def reset(self):
        """Finger lost or sensor moved: stored pulse shapes no longer apply."""
        self.cuffs = []
        self.gain = (1.0, 1.0)
        self.hist, self.pred_hist = [], {}
        self.last_est_sbp = self.confirm_due_ms = None
        self.pending_since_ms = self.held = None
        if self.kf is not None:
            self.kf.reset()

    # ------------------------------------------------------------- estimate
    def estimate(self, times_ms, raw_ir, news2_total: int | None = None) -> dict:
        c = self.config
        if not self.cuffs:
            return {"status": "NEEDS_CALIBRATION"}
        w = resample_25hz(times_ms, raw_ir, c.window_s)
        if w is None:
            return {"status": "COLLECTING"}
        f = extract_sensor(w)
        if np.isnan(f).all() or not np.isfinite(f[-2]) or not bool(ok_estimate(f, self.gate)):
            return {"status": "POOR_SIGNAL"}

        t_now = float(times_ms[-1])
        P = self._predict_change(f, self.cuffs, t_now)
        self._remember(t_now, f)
        if self.smooth_s > 0:          # causal mean of each cuff's recent change predictions
            for k, cf in enumerate(self.cuffs):
                h = self.pred_hist.setdefault(cf.t_ms, [])
                h.append((t_now, P[k].copy()))
                h[:] = [(t, v) for t, v in h if t > t_now - self.smooth_s * 1000]
                P[k] = np.mean([v for _, v in h], axis=0)
        if self.half_life:             # newer cuffs count more
            age = np.array([(t_now - cf.t_ms) / 60000 for cf in self.cuffs])
            w = 0.5 ** (age / float(self.half_life))
        else:
            w = np.ones(len(self.cuffs))
        w = w / w.sum()
        sv = np.array([cf.sbp for cf in self.cuffs]) + self.gain[0] * P[:, 0]
        dv = np.array([cf.dbp for cf in self.cuffs]) + self.gain[1] * P[:, 1]
        sbp, dbp = float((w * sv).sum()), float((w * dv).sum())
        last = self.cal
        age_min = (times_ms[-1] - last.t_ms) / 60000
        sigma = None
        if self.kf is not None:        # fuse with the cuffs; sigma grows with time since the cuff
            x, sg = self.kf.ppg(t_now / 1000, [sbp, dbp], age_min)
            sbp, dbp, sigma = float(x[0]), float(x[1]), sg
        d_sbp, d_dbp = sbp - last.sbp, dbp - last.dbp
        # NEWS2 SBP score for the dashboard: cautious (estimate - z * sigma) in Kalman mode
        score_sbp = sbp - self.score_z * float(sigma[0]) if sigma is not None else sbp
        news2_shown = news2_sbp_score(score_sbp)

        single = len(self.cuffs) == 1
        drop_thr = self.alerts.get("drop_alert_mmhg", c.trend_alert_mmhg)
        hypo_thr = self.alerts.get("hypotension_alert_sbp")
        if single:   # single-cuff thresholds when the settings file has them
            drop_thr = self.alerts.get("drop_alert_mmhg_single_cuff", drop_thr)
            hypo_thr = self.alerts.get("hypotension_alert_sbp_single_cuff", hypo_thr)

        if self.schedule == "kalman":
            limit = c.max_interval_kalman_min
        else:
            limit = c.max_interval_unstable_min if (news2_total or 0) >= 5 else c.max_interval_stable_min
        reasons = []
        if age_min >= limit:
            reasons.append("INTERVAL")
        if -d_sbp >= drop_thr:
            reasons.append("DROP_ALERT")
        if self.schedule == "kalman":
            if news2_shown > news2_sbp_score(last.sbp):
                reasons.append("UNCERTAINTY")
        else:
            change = -d_sbp if c.smart_drop_only else abs(d_sbp)
            if change >= self.trigger_mmhg:
                reasons.append("SMART_CHANGE")
        if d_sbp >= c.trend_alert_mmhg:
            reasons.append("RISE_ALERT")
        if hypo_thr is not None and sbp <= hypo_thr:
            reasons.append("HYPOTENSION_ALERT")
        if self.schedule != "kalman" and c.recal_on_band_change and news2_sbp_score(sbp) != news2_sbp_score(last.sbp):
            if not c.smart_drop_only or news2_sbp_score(sbp) > news2_sbp_score(last.sbp):
                reasons.append("NEWS2_BAND_CHANGE")
        if self.confirm_due_ms is not None and t_now >= self.confirm_due_ms:
            reasons.append("CONFIRM")
        hr_cal, hr_now = last.features[-6], f[-6]   # hr_bpm
        if np.isfinite(hr_cal) and np.isfinite(hr_now) and abs(hr_now - hr_cal) / hr_cal >= c.hr_change_frac:
            reasons.append("HR_CHANGE")

        if self.schedule == "kalman":
            # validated rule: only the interval and the uncertainty trigger ask for a cuff;
            # alerts (drop, hypotension, rise, HR) are still reported for the crew
            cuff_now = "INTERVAL" in reasons or "CONFIRM" in reasons or \
                ("UNCERTAINTY" in reasons and age_min >= c.min_cuff_gap_min)
        else:
            cuff_now = "INTERVAL" in reasons or (bool(reasons) and age_min >= c.min_cuff_gap_min)
        # cuff request bookkeeping: keep asking until calibrate(); optionally hold what is shown
        live_sbp, live_dbp = sbp, dbp
        pending_min = None
        if self.pending_since_ms is None and cuff_now:
            self.pending_since_ms = t_now
            self.held = (sbp, dbp, news2_shown, sigma)
        elif self.pending_since_ms is not None:
            pending_min = (t_now - self.pending_since_ms) / 60000
            cuff_now = True
            if pending_min >= c.cuff_overdue_min:
                reasons.append("CUFF_OVERDUE")
            if c.pending_display == "hold_estimate" and self.held is not None:
                sbp, dbp, news2_shown, sigma = self.held
            elif c.pending_display == "hold_cuff":
                sbp, dbp, news2_shown = last.sbp, last.dbp, news2_sbp_score(last.sbp)
        held_now = pending_min is not None and c.pending_display != "live"
        out = {
            "status": "STALE" if age_min >= c.stale_after_min else ("AWAITING_CUFF" if pending_min is not None else "OK"),
            "sbp": round(sbp, 1), "dbp": round(dbp, 1),
            "held": held_now, "live_sbp": round(live_sbp, 1), "live_dbp": round(live_dbp, 1),
            "awaiting_cuff_min": None if pending_min is None else round(pending_min, 1),
            "news2_sbp_score": news2_shown,
            "delta_sbp": round(float(d_sbp), 1), "delta_dbp": round(float(d_dbp), 1),
            "minutes_since_cuff": round(float(age_min), 1),
            "cuffs_used": len(self.cuffs), "patient_gain_sbp": round(self.gain[0], 2),
            "recalibrate": bool(reasons), "recalibrate_reasons": reasons,
            # cuff_now: what the dashboard should act on. INTERVAL always counts;
            # change-based reasons wait until min_cuff_gap_min has passed.
            "cuff_now": bool(cuff_now),
            "next_cuff_due_min": round(max(limit - age_min, 0.0), 1),
            "mode": self.schedule,
        }
        self.last_est_sbp = live_sbp
        if sigma is not None:
            out["sbp_sigma"] = round(float(sigma[0]), 1)
            out["sbp_range_95"] = [round(sbp - 1.96 * float(sigma[0]), 1), round(sbp + 1.96 * float(sigma[0]), 1)]
            out["news2_score_z"] = self.score_z
        return out


def save_config(path: str | Path, config: CalibrationConfig) -> None:
    Path(path).write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
