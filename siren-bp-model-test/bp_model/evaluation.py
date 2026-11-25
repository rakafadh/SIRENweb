"""Shared accuracy and alert metrics for the calibrated BP scripts."""
from __future__ import annotations

import numpy as np
from sklearn.metrics import roc_auc_score

from .calibrated import news2_sbp_score


def metrics(true_abs, pred_abs, cal_abs, true_d, pred_d) -> dict:
    out = {}
    for i, k in enumerate(["SBP", "DBP"]):
        e = pred_abs[:, i] - true_abs[:, i]
        out[k] = {
            "ME": float(e.mean()), "SD": float(e.std(ddof=1)), "MAE": float(np.abs(e).mean()),
            "within5": float(np.mean(np.abs(e) <= 5) * 100),
            "within10": float(np.mean(np.abs(e) <= 10) * 100),
            "within15": float(np.mean(np.abs(e) <= 15) * 100),
            "r_change": float(np.corrcoef(true_d[:, i], pred_d[:, i])[0, 1]) if np.std(pred_d[:, i]) > 0 else 0.0,
            "ISO": "PASS" if abs(e.mean()) <= 5 and e.std(ddof=1) <= 8 else "FAIL",
        }
    tb = np.array([news2_sbp_score(v) for v in true_abs[:, 0]])
    pb = np.array([news2_sbp_score(v) for v in pred_abs[:, 0]])
    low_t, low_p = tb >= 2, pb >= 2
    drop_t, drop_p = true_d[:, 0] <= -20, pred_d[:, 0] <= -20
    big = np.abs(true_d[:, 0]) >= 10
    out["NEWS2_band_agree"] = float(np.mean(tb == pb) * 100)
    out["SBP<=100_sens"] = float(np.mean(low_p[low_t]) * 100) if low_t.any() else float("nan")
    out["SBP<=100_spec"] = float(np.mean(~low_p[~low_t]) * 100) if (~low_t).any() else float("nan")
    out["drop20_sens"] = float(np.mean(drop_p[drop_t]) * 100) if drop_t.any() else float("nan")
    out["drop20_false_alarm"] = float(np.mean(drop_p[~drop_t]) * 100) if (~drop_t).any() else float("nan")
    # Direction of change agreement when the true change is at least 10 mmHg
    out["direction_agree"] = float(np.mean(np.sign(pred_d[big, 0]) == np.sign(true_d[big, 0])) * 100) if big.any() else float("nan")
    return out


def table(rows: list[tuple[str, dict]]) -> str:
    h = ("| Model | SBP MAE | SBP ME | SBP SD | ±10 mmHg | r (change) | DBP MAE | DBP SD | ISO SBP/DBP "
         "| NEWS2 band | SBP≤100 sens | 20 mmHg drop sens | drop false alarm | direction |\n"
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for name, m in rows:
        s, d = m["SBP"], m["DBP"]
        h += (f"| {name} | {s['MAE']:.2f} | {s['ME']:+.2f} | {s['SD']:.2f} | {s['within10']:.1f}% | {s['r_change']:.2f} "
              f"| {d['MAE']:.2f} | {d['SD']:.2f} | {s['ISO']}/{d['ISO']} | {m['NEWS2_band_agree']:.1f}% "
              f"| {m['SBP<=100_sens']:.1f}% | {m['drop20_sens']:.1f}% | {m['drop20_false_alarm']:.1f}% "
              f"| {m['direction_agree']:.1f}% |\n")
    return h




def alert_curve(event: np.ndarray, score: np.ndarray, thresholds) -> list[dict]:
    """Sensitivity and false alarm rate when alerting at score >= threshold."""
    out = []
    for t in thresholds:
        a = score >= t
        out.append({"threshold": float(t),
                    "sens": float(np.mean(a[event]) * 100) if event.any() else float("nan"),
                    "false_alarm": float(np.mean(a[~event]) * 100) if (~event).any() else float("nan"),
                    "ppv": float(np.mean(event[a]) * 100) if a.any() else float("nan")})
    return out


def auroc(event: np.ndarray, score: np.ndarray) -> float:
    if event.all() or not event.any() or np.std(score) == 0:
        return float("nan")
    return float(roc_auc_score(event, score))


def pick_threshold(event: np.ndarray, score: np.ndarray, target_sens: float) -> float:
    """Highest threshold that still reaches target_sens (percent) on this data."""
    s = np.sort(score[event])[::-1]
    if s.size == 0:
        return float("nan")
    k = int(np.ceil(target_sens / 100 * s.size)) - 1
    return float(s[min(max(k, 0), s.size - 1)])


def threshold_for_false_alarm(event: np.ndarray, score: np.ndarray, max_fa: float) -> float:
    """Lowest threshold whose false alarm rate (percent of non-events alerted)
    stays at or below max_fa on this data."""
    neg = np.sort(score[~event])
    if neg.size == 0:
        return float("nan")
    k = int(np.floor(neg.size * (1 - max_fa / 100)))
    return float(neg[min(k, neg.size - 1)]) + 1e-9
