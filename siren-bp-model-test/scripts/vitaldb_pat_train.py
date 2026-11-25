"""Does pulse arrival time (ECG + PPG) catch the BP drops pulse shape misses?

    python scripts/vitaldb_pat_download.py --n-cases 150      # first
    python scripts/vitaldb_pat_train.py

The same script tests any extra feature set saved as "P" next to "F", e.g. the
breathing-variation features:
    python scripts/vitaldb_resp_download.py --n-cases 300
    python scripts/vitaldb_pat_train.py --cache vitaldb_resp_cache --extra-label "PPG + breathing variation" --report vitaldb_resp_comparison.md

Trains two cuff-calibrated models on exactly the same VitalDB cases, pairs
and settings (layout v2, gradient boosting, band-balanced weights):
  PPG only    26 pulse-shape features (what SIREN has now)
  PPG + PAT   the same plus 6 PAT features from ECG lead II (bp_model/pat.py)
and compares them on the same test patients:
  1. single-cuff accuracy and drop / hypotension detection,
  2. whether PAT moves with SBP at all (correlation of changes),
  3. NEWS2 usability for fixed 5 min, fixed 10 min and 10 min + any 8 mmHg /
     band change, 5 cuff-error repeats, 95% CI by resampling patients.
Writes results/vitaldb_pat_comparison.md. With ~150 cases both models see
fewer training patients than the 728-case model, so compare the two columns
with each other, not with earlier tables.
"""
import _path  # noqa: F401
import argparse
import os
import time
from pathlib import Path

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import numpy as np
from scipy.stats import spearmanr

from bp_model.evaluation import metrics, table, threshold_for_false_alarm
from bp_model.features_v2 import FEATURE_VERSION
from bp_model.pat import N_PAT, PAT_NAMES
from robustness_check import counts
from vitaldb_train import (SQI, band_weights, build, fit, hgb, pair_index, recent_features, schedule_metrics,
                           schedule_sim, split_cases)

ROOT = Path(__file__).resolve().parent.parent


def load_pat_cases(cache: Path):
    """Two parallel case lists: PPG features only, and PPG + extra (PAT or other) features.
    Also returns the extra feature names."""
    ppg, both, names = [], [], list(PAT_NAMES)
    for p in sorted(cache.glob("*.npz")):
        z = np.load(p, allow_pickle=False)
        if "empty" in z.files or str(z["version"]) != FEATURE_VERSION or len(z["t"]) < 20:
            continue
        F, P = z["F"], z["P"]
        if "extra_names" in z.files:
            names = [str(x) for x in z["extra_names"]]
        base = {"caseid": int(z["caseid"]), "subject": int(z["subjectid"]), "t": z["t"], "L": z["L"],
                "nibp": z["nibp"], "sqi": np.nan_to_num(F[:, SQI], nan=0.0), "P": P}
        a = dict(base, F=F)
        a["Frec"] = recent_features(a, 0.80)
        b = dict(base, F=np.hstack([F, P]))
        b["Frec"] = recent_features(b, 0.80)
        ppg.append(a)
        both.append(b)
    return ppg, both, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_pat_cache"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seeds", type=int, default=5, help="cuff-error repeats for the NEWS2 check")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--cuff-noise", type=float, default=6.0)
    ap.add_argument("--smart-trigger", type=float, default=8.0)
    ap.add_argument("--max-false-alarm", type=float, default=20.0)
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--extra-label", default="PPG + PAT (ECG lead II)", help="name of the second model")
    ap.add_argument("--report", default="vitaldb_pat_comparison.md")
    args = ap.parse_args()
    T0 = time.time()
    rng = np.random.default_rng(args.seed)
    ppg, both, extra_names = load_pat_cases(Path(args.cache))
    xlab = args.extra_label
    xshort = xlab.split(" (")[0]
    if len(ppg) < 10:
        raise SystemExit(f"only {len(ppg)} usable cases in {args.cache}; run vitaldb_pat_download.py first")
    sp = {"ppg": split_cases(ppg, args.seed), "pat": split_cases(both, args.seed)}
    n = {k: len(v) for k, v in sp["ppg"].items()}
    pat_ok = float(np.mean(np.concatenate([np.isfinite(c["P"][:, 0]) for c in sp["ppg"]["test"]])) * 100)
    print(f"cases {n}; test windows with extra features {pat_ok:.0f}%", flush=True)

    # same pairs for both models (pairs depend only on times and PPG quality)
    tr_idx = pair_index(sp["ppg"]["train"], 120, 900, 20, rng, True, 0.90, 0.80)
    te_idx = pair_index(sp["ppg"]["test"], 120, 900, None, rng, True, 0.90, 0.80)
    models, rows = {}, []
    for key, label in (("ppg", "PPG only (pulse shape)"), ("pat", xlab)):
        X, Y, C, _ = build(sp[key]["train"], tr_idx, "v2")
        models[key] = fit(hgb(args.seed), X, Y, C, 3.0, weights=band_weights(Y, C, "inverse"))
        Xt, Yt, Ct, Gt = build(sp[key]["test"], te_idx, "v2")
        pred = models[key].predict(Xt)
        rows.append((label, metrics(Ct + Yt, Ct + pred, Ct, Yt, pred)))
        print(f"  {label:<28} trained on {len(X)} pairs; test SBP MAE {rows[-1][1]['SBP']['MAE']:.2f}  "
              f"[{time.time() - T0:.0f} s]", flush=True)
    rows.insert(0, ("Hold last cuff value", metrics(Ct + Yt, Ct, Ct, Yt, np.zeros_like(Yt))))

    # does PAT move with SBP? (test pairs, change since the cuff)
    corr = {}
    for j, name in enumerate(extra_names):
        d_pat, d_sbp = [], []
        for ci, i, k in te_idx:
            c = sp["ppg"]["test"][ci]
            if np.isfinite(c["P"][i, j]) and np.isfinite(c["P"][k, j]):
                d_pat.append(c["P"][k, j] - c["P"][i, j])
                d_sbp.append(c["L"][k, 0] - c["L"][i, 0])
        if len(d_pat) > 50:
            corr[name] = (spearmanr(d_pat, d_sbp).correlation, len(d_pat))

    # NEWS2 usability, same schedules and cuff errors for both models
    crit = {"within1": 95.0, "max_miss": 5.0, "min_hypo_sens": 80.0}
    rng_b = np.random.default_rng(0)
    sched = [("Fixed every 5 min", 5, None), ("Fixed every 10 min", 10, None),
             (f"10 min + extra cuff on any {args.smart_trigger:g} mmHg change or NEWS2 band change", 10, args.smart_trigger)]
    news2 = []
    for key, label in (("ppg", "PPG only"), ("pat", xshort)):
        Tv, Ev, _, _ = schedule_sim(sp[key]["val"], models[key], 5, None, args.cuff_noise, np.random.default_rng(7), 5.0)
        hypo_thr = -threshold_for_false_alarm(Tv[:, 0] <= 100, -Ev[:, 0], args.max_false_alarm)
        for sl, base, trig in sched:
            Ts, Es, Ps, ncs, pooled = [], [], [], [], None
            for sd in range(args.seeds):
                T, E, _, nc, x = schedule_sim(sp[key]["test"], models[key], base, trig, args.cuff_noise,
                                              np.random.default_rng(100 + sd), 5.0, extra=True)
                Ts.append(T); Es.append(E); Ps.append(x["band"]); ncs.append(nc.mean())
                ids, cnt = counts(T, E, x["band"], x["subject"], hypo_thr)
                pooled = {k: [v[0].copy(), v[1].copy()] for k, v in cnt.items()} if pooled is None else \
                    {k: [pooled[k][0] + v[0], pooled[k][1] + v[1]] for k, v in cnt.items()}
            m = schedule_metrics(np.concatenate(Ts), np.concatenate(Es), hypo_thr, crit, PB=np.concatenate(Ps))
            nm, dm = pooled["miss"]
            idx = rng_b.integers(0, len(nm), size=(args.boot, len(nm)))
            bs = 100 * nm[idx].sum(1) / np.maximum(dm[idx].sum(1), 1e-9)
            news2.append((label, sl, float(np.mean(ncs)), m, np.percentile(bs, 2.5), np.percentile(bs, 97.5)))
            print(f"  {label:<10} {sl:<62} cuffs/15 min {np.mean(ncs):.2f}  dangerous miss {m['miss']:.1f}% "
                  f"(95% CI {news2[-1][4]:.1f}-{news2[-1][5]:.1f})  within 1 band {m['band_within1']:.1f}%  "
                  f"NEWS2 usable {'YES' if m['news2_usable'] else 'no'}  [{time.time() - T0:.0f} s]", flush=True)

    md = [f"# Extra feature test: {xlab} (VitalDB)\n\nCases {n} (split by patient, "
          f"same split as vitaldb_train.py). Test windows with the extra features: {pat_ok:.0f}%. Both models: layout v2, "
          "gradient boosting, band-balanced weights, same pairs. Compare the two models with each other; with fewer "
          "training patients the absolute numbers are worse than the 728-case model.\n\n"
          "## 1. Single cuff (test pairs, 15 s to 15 min after a cuff)\n\n", table(rows),
          "\n## 2. Does each extra feature move with SBP? (Spearman correlation of change since the cuff, test pairs)\n\n"
          "Expected negative for both sets: PAT shortens when SBP rises; breathing variation grows when SBP falls "
          "from low volume.\n\n| Feature | r | pairs |\n|---|---|---|\n"]
    md += [f"| {k} | {r:.2f} | {nn} |\n" for k, (r, nn) in corr.items()]
    md.append(f"\n## 3. NEWS2 usability ({args.seeds} cuff-error repeats, SD {args.cuff_noise:g} mmHg; 95% CI of "
              "dangerous miss by resampling patients; hypotension alert threshold tuned on validation per model)\n\n"
              "| Model | Schedule | Cuffs per 15 min | SBP MAE | Within one band | Dangerous miss (95% CI) | "
              "Hypotension alert sens | Over-triage | NEWS2 usable |\n|---|---|---|---|---|---|---|---|---|\n")
    for label, sl, ncm, m, lo, hi in news2:
        md.append(f"| {label} | {sl} | {ncm:.1f} | {m['MAE']:.2f} | {m['band_within1']:.1f}% | {m['miss']:.1f}% "
                  f"({lo:.1f}-{hi:.1f}) | {m['hypo_sens']:.1f}% | {m['overcall']:.1f}% | "
                  f"**{'YES' if m['news2_usable'] else 'NO'}** |\n")
    md.append("\nVitalDB patients are anaesthetised and ventilated; drugs change the pre-ejection period and vessel "
              "tone, and ventilation drives the breathing variation, so either result still needs a SIREN volunteer "
              "test.\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / args.report).write_text("".join(md), encoding="utf-8")
    print("".join(md))
    print(f"written {out / args.report}  [{time.time() - T0:.0f} s]")


if __name__ == "__main__":
    main()
