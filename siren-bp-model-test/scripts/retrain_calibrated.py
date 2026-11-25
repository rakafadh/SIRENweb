"""Retrain the BP model as a cuff-calibrated trend model and compare ML models.

    python scripts/retrain_calibrated.py --dataset "D:/dataverse_files"

Steps
  1. Features. Every MIMIC-BP window is brought to 25 Hz (the MAX30102 rate)
     and turned into 26 pulse-shape features (bp_model/features_v2.py).
     Cached in results/features_morph25.npz, so later runs skip this.
  2. Pairs. Inside each patient, a "calibration" window (its BP plays the cuff
     reading) is paired with every other window. Target = change in BP.
  3. Models. Several regressors learn the change. Baselines are included so
     every number has something to beat.
  4. Evaluation on test patients, calibrating on each of their first
     --eval-cals windows in turn. Metrics include ISO 81060-2, NEWS2 band
     agreement, SBP <= 100 detection and 20 mmHg drop alerts.
  5. The best model on the validation split is saved to
     model/bp_calibrated.joblib with the calibration settings.

Patients never appear in more than one split (official MIMIC-BP split), so
there is no leakage between training and test.

Note on timing. MIMIC-BP segments are spread over each ICU stay and are not
consecutive, so the gap between calibration and test window is often hours.
An ambulance run with a cuff every 10-15 min is an easier case than this.
"""
import _path  # noqa: F401
import argparse
import ast
import json
import time
from dataclasses import asdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.multioutput import MultiOutputRegressor
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from bp_model.calibrated import CalibrationConfig, pair_features
from bp_model.evaluation import metrics, table
from bp_model.features_v2 import FEATURE_NAMES, FEATURE_VERSION, N_FEATURES, extract_mimic

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- data
def parse_subjects(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        v = ast.literal_eval(raw)
        if isinstance(v, list):
            return [str(s).strip() for s in v]
    except (ValueError, SyntaxError):
        pass
    return [s.strip() for s in raw.splitlines() if s.strip()]


def _subject_features(ppg_path: str) -> np.ndarray:
    import warnings
    warnings.simplefilter("ignore")
    return np.array([extract_mimic(w) for w in np.load(ppg_path)])


def build_feature_cache(ds: Path, cache: Path, jobs: int, limit: int | None) -> dict:
    split_of = {}
    for split in ("train", "val", "test"):
        for s in parse_subjects(ds / f"{split}_subjects.txt"):
            split_of[s] = split
    subjects = [s for s in split_of
                if (ds / "ppg" / f"{s}_ppg.npy").exists() and (ds / "labels" / f"{s}_labels.npy").exists()]
    if limit:
        subjects = subjects[:limit]

    old = {}
    if cache.exists():
        z = np.load(cache, allow_pickle=False)
        if str(z["version"]) == FEATURE_VERSION:
            old = {s: (f, l) for s, f, l in zip(z["subjects"], z["features"], z["labels"])}
    todo = [s for s in subjects if s not in old]
    print(f"{len(subjects)} subjects found, {len(old)} cached, {len(todo)} to extract")

    if todo:
        from joblib import Parallel, delayed
        t0 = time.time()
        feats = Parallel(n_jobs=jobs, verbose=5)(
            delayed(_subject_features)(str(ds / "ppg" / f"{s}_ppg.npy")) for s in todo)
        for s, f in zip(todo, feats):
            old[s] = (f, np.load(ds / "labels" / f"{s}_labels.npy").astype(np.float64))
        print(f"feature extraction {time.time() - t0:.0f} s")
        keys = sorted(old)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, version=FEATURE_VERSION, subjects=np.array(keys),
                            features=np.stack([old[k][0] for k in keys]),
                            labels=np.stack([old[k][1] for k in keys]))

    data = {"train": [], "val": [], "test": []}
    for s in subjects:
        f, l = old[s]
        data[split_of[s]].append((s, f, l))
    return data


def make_pairs(subjects, cal_idx_fn, rng=None, max_targets=None):
    X, Y, G, ABS = [], [], [], []
    for gi, (sid, F, L) in enumerate(subjects):
        ok = ~np.isnan(F).all(axis=1)
        idx = np.where(ok)[0]
        if idx.size < 3:
            continue
        for c in cal_idx_fn(idx, rng):
            targets = idx[idx != c]
            if max_targets and targets.size > max_targets:
                targets = rng.choice(targets, max_targets, replace=False)
            for j in targets:
                X.append(pair_features(F[j], F[c], L[c, 0], L[c, 1]))
                Y.append(L[j] - L[c])
                ABS.append(np.concatenate([L[j], L[c]]))
                G.append(gi)
    return np.array(X), np.array(Y), np.array(G), np.array(ABS)


# -------------------------------------------------------------- models
def model_zoo(seed: int):
    imp = lambda: SimpleImputer(strategy="median")  # noqa: E731
    return {
        "Ridge (linear)": make_pipeline(imp(), StandardScaler(), Ridge(alpha=10.0)),
        "Random forest": make_pipeline(imp(), RandomForestRegressor(
            n_estimators=200, max_depth=14, min_samples_leaf=20, max_features=0.4, n_jobs=-1, random_state=seed)),
        "Extra trees": make_pipeline(imp(), ExtraTreesRegressor(
            n_estimators=300, max_depth=16, min_samples_leaf=20, max_features=0.5, n_jobs=-1, random_state=seed)),
        "Gradient boosting (HistGB)": MultiOutputRegressor(HistGradientBoostingRegressor(
            max_iter=500, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40,
            l2_regularization=1.0, early_stopping=True, validation_fraction=0.1, random_state=seed)),
        "Neural net (MLP)": make_pipeline(imp(), StandardScaler(), MLPRegressor(
            hidden_layer_sizes=(64, 32), alpha=1e-3, learning_rate_init=1e-3, max_iter=200,
            early_stopping=True, random_state=seed)),
    }


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--jobs", type=int, default=-1, help="CPU cores for feature extraction")
    ap.add_argument("--limit", type=int, help="use only the first N subjects (quick test)")
    ap.add_argument("--train-cals", type=int, default=6, help="calibration windows per training patient")
    ap.add_argument("--eval-cals", type=int, default=5, help="calibration windows per test patient")
    ap.add_argument("--low-bp-weight", type=float, default=3.0,
                    help="training weight for windows with SBP <= 110 (1 = off)")
    ap.add_argument("--use-cal-bp", action="store_true",
                    help="also feed the cuff value itself to the model (lets it learn regression to the "
                         "population mean, which biases hypotensive patients upwards; off by default)")
    ap.add_argument("--no-reference", action="store_true", help="skip the cuff-value-as-input reference row")
    ap.add_argument("--models", default="all", help="comma list of model names, or all")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--resplit", help="ignore the official split and re-split by patient, e.g. 0.6,0.2,0.2 "
                    "(only for a partial copy of the dataset)")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(exist_ok=True, parents=True)
    rng = np.random.default_rng(args.seed)
    data = build_feature_cache(Path(args.dataset), out / "features_morph25.npz", args.jobs, args.limit)
    if args.resplit:
        fr = [float(v) for v in args.resplit.split(",")]
        pool = data["train"] + data["val"] + data["test"]
        order = np.random.default_rng(args.seed).permutation(len(pool))
        a, b = int(fr[0] * len(pool)), int((fr[0] + fr[1]) * len(pool))
        data = {"train": [pool[i] for i in order[:a]], "val": [pool[i] for i in order[a:b]],
                "test": [pool[i] for i in order[b:]]}
    print({k: len(v) for k, v in data.items()}, "patients per split")

    train_cal = lambda idx, r: r.choice(idx, min(args.train_cals, idx.size), replace=False)  # noqa: E731
    eval_cal = lambda idx, r: idx[:args.eval_cals]  # noqa: E731  first windows, fixed
    Xtr, Ytr, _, _ = make_pairs(data["train"], train_cal, rng)
    Xva, Yva, _, Ava = make_pairs(data["val"], eval_cal, rng)
    Xte, Yte, _, Ate = make_pairs(data["test"], eval_cal, rng)
    print(f"pairs train {len(Xtr)}  val {len(Xva)}  test {len(Xte)}")
    n_in = Xtr.shape[1] if args.use_cal_bp else Xtr.shape[1] - 2
    Mtr, Mva, Mte = Xtr[:, :n_in], Xva[:, :n_in], Xte[:, :n_in]

    rows_val, rows_test, fitted = [], [], {}

    def evaluate(name, abs_v, chg_v, abs_t, chg_t):
        """abs = estimated BP; chg = the change the method itself predicts since
        calibration (for uncalibrated models: estimate now minus estimate at the
        calibration window), so trend metrics are fair to every method."""
        mv = metrics(Ava[:, :2], abs_v, Ava[:, 2:], Yva, chg_v)
        mt = metrics(Ate[:, :2], abs_t, Ate[:, 2:], Yte, chg_t)
        rows_val.append((name, mv)); rows_test.append((name, mt))
        print(f"  {name:<34} val SBP MAE {mv['SBP']['MAE']:.2f} | test SBP MAE {mt['SBP']['MAE']:.2f} "
              f"SD {mt['SBP']['SD']:.2f}  DBP MAE {mt['DBP']['MAE']:.2f}")

    # Baseline 1: hold the cuff value (predict no change)
    evaluate("Baseline: hold last cuff value", Ava[:, 2:], np.zeros_like(Yva), Ate[:, 2:], np.zeros_like(Yte))

    # Baseline 2: the old model (absolute BP, no calibration), on the same test windows
    try:
        from bp_model import BpModel
        old = BpModel()
        ds = Path(args.dataset)

        def old_pred(subjects):
            P, C = [], []
            for sid, F, L in subjects:
                ppg = np.load(ds / "ppg" / f"{sid}_ppg.npy")
                idx = np.where(~np.isnan(F).all(axis=1))[0]
                if idx.size < 3:
                    continue
                pw = old.predict_many(ppg)
                for c in idx[:args.eval_cals]:
                    for j in idx[idx != c]:
                        P.append(pw[j]); C.append(pw[j] - pw[c])
            return np.array(P), np.array(C)
        pv, cv = old_pred(data["val"]); pt, ct = old_pred(data["test"])
        evaluate("Old model (RF, no calibration)", pv, cv, pt, ct)
    except Exception as exc:  # old model or its files missing
        print("  old model skipped:", exc)

    # Baseline 3: new features, but absolute BP without calibration
    nf = N_FEATURES
    abs_model = MultiOutputRegressor(HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=args.seed))
    tr_abs_y = Ytr + Xtr[:, -2:]
    abs_model.fit(Xtr[:, :nf], tr_abs_y)
    def abs_eval(X):
        now = abs_model.predict(X[:, :nf])
        at_cal = abs_model.predict(X[:, :nf] - X[:, nf:2 * nf])   # f_cal = f_now - (f_now - f_cal)
        return now, now - at_cal
    pv, cv = abs_eval(Xva); pt, ct = abs_eval(Xte)
    evaluate("New features, no calibration (HistGB)", pv, cv, pt, ct)

    # Calibrated models
    zoo = model_zoo(args.seed)
    wanted = zoo if args.models == "all" else {k: v for k, v in zoo.items() if k in args.models.split(",")}
    for name, model in wanted.items():
        t0 = time.time()
        fit_kw = {}
        if args.low_bp_weight != 1 and "MLP" not in name:
            w = np.where(Ytr[:, 0] + Xtr[:, -2] <= 110, args.low_bp_weight, 1.0)
            fit_kw = ({f"{model.steps[-1][0]}__sample_weight": w} if hasattr(model, "steps") else {"sample_weight": w})
        model.fit(Mtr, Ytr, **fit_kw)
        fitted[name] = model
        dv, dt = model.predict(Mva), model.predict(Mte)
        evaluate(f"Calibrated: {name}", Ava[:, 2:] + dv, dv, Ate[:, 2:] + dt, dt)
        print(f"    fit {time.time() - t0:.0f} s")

    # For comparison: the same idea, but the model also sees the cuff value.
    # It then learns "high readings tend to come down, low ones go up" from
    # MIMIC's hours-apart segments. Looks better on average, but it pulls
    # hypotensive patients towards normal, so it is reported, not saved.
    if not args.use_cal_bp and not args.no_reference:
        t0 = time.time()
        et = model_zoo(args.seed)["Extra trees"]
        w = np.where(Ytr[:, 0] + Xtr[:, -2] <= 110, args.low_bp_weight, 1.0)
        et.fit(Xtr, Ytr, **{f"{et.steps[-1][0]}__sample_weight": w})
        dv, dt = et.predict(Xva), et.predict(Xte)
        evaluate("Reference: Extra trees + cuff value as input", Ava[:, 2:] + dv, dv, Ate[:, 2:] + dt, dt)
        print(f"    fit {time.time() - t0:.0f} s")

    # Pick on validation SBP MAE, among calibrated models only
    cal_rows = [(n, m) for n, m in rows_val if n.startswith("Calibrated: ")]
    best = min(cal_rows, key=lambda r: r[1]["SBP"]["MAE"])[0].replace("Calibrated: ", "")
    best_test = dict(rows_test)[f"Calibrated: {best}"]
    print(f"\nBest on validation: {best}")

    # Feature importance for the tree models, by permutation-free proxy
    names = (FEATURE_NAMES + [f"d_{n}" for n in FEATURE_NAMES] + ["sbp_cal", "dbp_cal"])[:n_in]
    importance = None
    m = fitted[best]
    est = m.steps[-1][1] if hasattr(m, "steps") else None
    if est is not None and hasattr(est, "feature_importances_"):
        imp = est.feature_importances_
        importance = sorted(zip(names, imp.tolist()), key=lambda t: -t[1])[:15]

    config = CalibrationConfig()
    joblib.dump({
        "model": m, "model_name": best, "feature_version": FEATURE_VERSION,
        "pair_layout": "x = [f_now(26), f_now - f_cal(26), sbp_cal, dbp_cal]; y = [d_sbp, d_dbp]",
        "n_inputs": n_in, "uses_cal_bp": bool(args.use_cal_bp),
        "config": asdict(config), "test_metrics": best_test,
        "n_train_patients": len(data["train"]),
    }, ROOT / "model" / "bp_calibrated.joblib")

    md = ("# Calibrated BP model, comparison\n\n"
          f"Patients: train {len(data['train'])}, val {len(data['val'])}, test {len(data['test'])}. "
          f"Test pairs: {len(Xte)} (first {args.eval_cals} windows of each test patient used as calibration).\n\n"
          "## Test split\n\n" + table(rows_test) +
          "\n## Validation split\n\n" + table(rows_val) +
          f"\nBest on validation: **{best}**, saved to model/bp_calibrated.joblib\n")
    if importance:
        md += "\n## Top features of the best model\n\n" + "\n".join(f"- {n}: {v:.3f}" for n, v in importance) + "\n"
    (out / "calibrated_comparison.md").write_text(md, encoding="utf-8")
    (out / "calibrated_comparison.json").write_text(json.dumps(
        {"test": rows_test, "val": rows_val, "best": best, "config": asdict(config)}, indent=2), encoding="utf-8")
    print("\n" + table(rows_test))
    print(f"written {out / 'calibrated_comparison.md'}")


if __name__ == "__main__":
    main()
