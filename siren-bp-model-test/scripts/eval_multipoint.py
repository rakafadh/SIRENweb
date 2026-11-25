"""Multi-point calibration and alert-threshold tuning for the calibrated model.

    python scripts/eval_multipoint.py --dataset "D:/dataverse_files"

Needs model/bp_calibrated.joblib (from retrain_calibrated.py) and the feature
cache results/features_morph25.npz (made by the same script).

What it simulates, per test patient
  - The first 4 windows are cuff readings, taken one after another.
  - Windows 5 to 30 are the moments SIREN has to estimate BP.
  - With 1, 2, 3 and 4 cuffs available, four methods are compared:
      hold       show the latest cuff value
      latest     latest cuff + model change (what retrain_calibrated.py measured)
      multi      mean over all cuffs of (cuff + model change)
      multi+gain multi, with the patient gain fitted from the cuffs (bp_model/multipoint.py)
  - The gain's shrinkage lam is chosen on the validation split, never on test.

Alerts, tuned on validation and reported on test
  - Drop alert: true SBP fell 20 mmHg or more below the latest cuff.
    The alert fires when the predicted drop reaches a threshold.
  - Hypotension alert: true SBP <= 100. Fires when the estimate falls to a threshold.
  Two operating points are tuned on validation: high sensitivity (--target-sens)
  and limited false alarms (--max-false-alarm). The second is saved, because
  an alert that fires on half of all windows would be ignored by the crew.
  Results go to results/multipoint_comparison.md and model/alert_settings.json.

MIMIC-BP windows are not in time order, so "first 4 windows" are 4 readings at
unknown times in the ICU stay. Gains and thresholds need checking on SIREN data.
"""
import _path  # noqa: F401
import argparse
import json
from pathlib import Path

import joblib
import numpy as np

from bp_model.calibrated import pair_features
from bp_model.evaluation import alert_curve, auroc, metrics, pick_threshold, table, threshold_for_false_alarm
from bp_model.multipoint import combine, fit_gain, pair_arrays
from retrain_calibrated import build_feature_cache

ROOT = Path(__file__).resolve().parent.parent
N_CUFF = 4
LAMBDAS = [10, 30, 100, 300, 1000, 3000, float("inf")]


def predict_patients(model, n_in, subjects):
    """Per patient: labels, cuff labels, and model change from each of the
    first N_CUFF windows to every window."""
    out, X, spans = [], [], []
    for sid, F, L in subjects:
        idx = np.where(~np.isnan(F).all(axis=1))[0]
        if idx.size < N_CUFF + 3:
            continue
        start = len(X)
        for i in idx[:N_CUFF]:
            for j in idx:
                X.append(pair_features(F[j], F[i], L[i, 0], L[i, 1])[:n_in])
        spans.append((start, len(X), idx, L))
    P_all = model.predict(np.array(X))
    for start, end, idx, L in spans:
        P = P_all[start:end].reshape(N_CUFF, idx.size, 2)
        out.append({"L": L[idx], "P": P})
    return out


def run_method(patients, method, n, lam):
    true_abs, est_abs, latest = [], [], []
    for p in patients:
        L, P = p["L"], p["P"]
        cuff = L[:n]
        tgt = slice(N_CUFF, None)
        est = np.zeros_like(L[tgt])
        for b in range(2):
            if method == "hold":
                e = np.full(L[tgt].shape[0], cuff[-1, b])
            elif method == "latest":
                e = cuff[-1, b] + P[n - 1, tgt, b]
            else:
                g = 1.0
                if method == "multi+gain" and n >= 2:
                    d, y = pair_arrays(cuff[:, b], P[:n, :n, b])
                    g = fit_gain(d, y, lam)
                e = combine(cuff[:, b], P[:n, tgt, b], g)
            est[:, b] = e
        true_abs.append(L[tgt]); est_abs.append(est)
        latest.append(np.repeat(cuff[-1:], L[tgt].shape[0], axis=0))
    t, e, c = np.vstack(true_abs), np.vstack(est_abs), np.vstack(latest)
    return t, e, c


def evaluate(patients, method, n, lam):
    t, e, c = run_method(patients, method, n, lam)
    return metrics(t, e, c, t - c, e - c), (t, e, c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--model", default=str(ROOT / "model" / "bp_calibrated.joblib"))
    ap.add_argument("--target-sens", type=float, default=80.0, help="high-sensitivity operating point, percent")
    ap.add_argument("--max-false-alarm", type=float, default=20.0,
                    help="saved operating point: most sensitive threshold with at most this false alarm rate, percent")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    out = Path(args.out)
    data = build_feature_cache(Path(args.dataset), out / "features_morph25.npz", -1, None)
    bundle = joblib.load(args.model)
    model, n_in = bundle["model"], bundle.get("n_inputs", 54)
    if hasattr(model, "n_jobs"):
        model.n_jobs = -1
    print(f"model: {bundle.get('model_name')}  patients val {len(data['val'])} test {len(data['test'])}")
    val = predict_patients(model, n_in, data["val"])
    test = predict_patients(model, n_in, data["test"])

    # 1. choose lam on validation (2 and 3 cuffs, SBP MAE)
    scores = {lam: np.mean([evaluate(val, "multi+gain", n, lam)[0]["SBP"]["MAE"] for n in (2, 3)]) for lam in LAMBDAS}
    lam = min(scores, key=scores.get)
    print("validation SBP MAE by lam:", {str(k): round(v, 3) for k, v in scores.items()}, "-> lam", lam)

    # 2. accuracy table on test
    rows, results = [], {}
    for n in range(1, N_CUFF + 1):
        for method in ("hold", "latest", "multi", "multi+gain"):
            if n == 1 and method in ("multi", "multi+gain"):
                continue
            m, arrays = evaluate(test, method, n, lam)
            rows.append((f"{n} cuff{'s' if n > 1 else ''}: {method}", m))
            results[(n, method)] = arrays
            print(f"  {n} cuffs {method:<11} SBP MAE {m['SBP']['MAE']:.2f} SD {m['SBP']['SD']:.2f} "
                  f"r {m['SBP']['r_change']:.2f}  DBP MAE {m['DBP']['MAE']:.2f}")

    # 3. alerts: tune on validation, report on test (1 cuff and 2 cuffs)
    alert_rows, settings = [], {}
    for n, method in ((1, "latest"), (2, "multi")):
        tv, ev, cv = run_method(val, method, n, lam)
        tt, et, ct = results[(n, method)]
        _, eh, _ = results[(n, "hold")]
        cases = {
            "drop >= 20 mmHg": ((tv - cv)[:, 0] <= -20, -(ev - cv)[:, 0], (tt - ct)[:, 0] <= -20, -(et - ct)[:, 0],
                                lambda t: f"predicted drop >= {t:.1f} mmHg", "hold cannot predict a drop"),
            "SBP <= 100": (tv[:, 0] <= 100, -ev[:, 0], tt[:, 0] <= 100, -et[:, 0],
                           lambda t: f"estimate <= {-t:.1f} mmHg", None),
        }
        settings[f"{n}_cuff"] = {}
        for label, (ev_v, sc_v, ev_t, sc_t, fmt, ref) in cases.items():
            if ref is None:
                ah = alert_curve(ev_t, -eh[:, 0], [-100.0])[0]
                ref = f"cuff <= 100: sens {ah['sens']:.1f}%, false alarm {ah['false_alarm']:.1f}%"
            auc = auroc(ev_t, sc_t)
            for point, thr in (("high sensitivity", pick_threshold(ev_v, sc_v, args.target_sens)),
                               ("limited false alarm", threshold_for_false_alarm(ev_v, sc_v, args.max_false_alarm))):
                a = alert_curve(ev_t, sc_t, [thr])[0]
                alert_rows.append((f"{n} cuff{'s' if n > 1 else ''}, {label}", point, auc, fmt(thr), a, ref))
                if point == "limited false alarm":
                    settings[f"{n}_cuff"]["drop" if "drop" in label else "hypo"] = round(thr if "drop" in label else -thr, 1)
        settings[f"{n}_cuff"]["drop_sweep_test"] = alert_curve((tt - ct)[:, 0] <= -20, -(et - ct)[:, 0], [3, 5, 8, 10, 15, 20])

    md = ["# Multi-point calibration and alerts\n",
          f"Model `{bundle.get('model_name')}`. Test patients {len(test)}; windows 1 to {N_CUFF} are cuff "
          f"readings, windows {N_CUFF + 1} to 30 are estimated. Patient-gain shrinkage lam = {lam} "
          f"(chosen on validation).\n",
          "## Accuracy on test\n", table(rows),
          f"\n## Alerts on test (thresholds tuned on validation: {args.target_sens:.0f}% sensitivity, "
          f"or at most {args.max_false_alarm:.0f}% false alarms)\n\n",
          "| Alert | Operating point | AUROC | Threshold | Sensitivity | False alarm | PPV | Cuff-only comparison |\n"
          "|---|---|---|---|---|---|---|---|\n"]
    for name, point, auc, thr, a, ref in alert_rows:
        md.append(f"| {name} | {point} | {auc:.2f} | {thr} | {a['sens']:.1f}% | {a['false_alarm']:.1f}% | {a['ppv']:.1f}% | {ref} |\n")
    md.append("\n## Drop alert at fixed thresholds (test)\n\n| Cuffs | Predicted drop >= | Sensitivity | False alarm | PPV |\n|---|---|---|---|---|\n")
    for key in ("1_cuff", "2_cuff"):
        for a in settings[key]["drop_sweep_test"]:
            md.append(f"| {key[0]} | {a['threshold']:.0f} mmHg | {a['sens']:.1f}% | {a['false_alarm']:.1f}% | {a['ppv']:.1f}% |\n")
    (out / "multipoint_comparison.md").write_text("".join(md), encoding="utf-8")

    save = {"gain_lambda": lam if np.isfinite(lam) else None, "max_anchors": 3,
            "max_false_alarm_pct": args.max_false_alarm,
            "drop_alert_mmhg": settings["2_cuff"]["drop"],
            "drop_alert_mmhg_single_cuff": settings["1_cuff"]["drop"],
            "hypotension_alert_sbp": settings["2_cuff"]["hypo"],
            "hypotension_alert_sbp_single_cuff": settings["1_cuff"]["hypo"]}
    (ROOT / "model" / "alert_settings.json").write_text(json.dumps(save, indent=2), encoding="utf-8")
    print("".join(md))
    print(f"written {out / 'multipoint_comparison.md'} and model/alert_settings.json")


if __name__ == "__main__":
    main()
