"""Step 3. Evaluate on the MIMIC-BP dataset, in three ways.

    python scripts/eval_mimic.py --dataset "E:/.../Despro/dataset"
    python scripts/eval_mimic.py --dataset sample_data/fake_mimic      (synthetic, checks the script runs)

Dataset layout, the same one train_bp_model.py uses:
    dataset/ppg/<subject>_ppg.npy        (windows, 3750)  125 Hz pleth
    dataset/labels/<subject>_labels.npy  (windows, 2)     SBP, DBP
    dataset/test_subjects.txt            subject ids

Modes
  patient   averages features and labels per patient, exactly as training did.
            Should reproduce bp_model_metrics.json.
  window    one prediction per 30 s window, which is how siren_app uses it.
  sensor    each window downsampled to 25 Hz and inverted like MAX30102 IR,
            then sent through prepare_window. Shows the cost of the sensor's
            lower sample rate (the domain gap), still on MIMIC patients.
"""
import _path  # noqa: F401
import argparse
import ast
import csv
import json
from pathlib import Path

import numpy as np

from bp_model import BpModel, extract_features, news2_sbp_score, prepare_window


def parse_subjects(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return [str(s).strip() for s in parsed if str(s).strip()]
    except (ValueError, SyntaxError):
        pass
    return [line.strip() for line in raw.splitlines() if line.strip()]


def as_sensor(window_125: np.ndarray, fs_sensor: float, reference: dict) -> np.ndarray | None:
    step = int(round(125.0 / fs_sensor))
    samples = window_125[::step]
    t_ms = np.arange(samples.size) * 1000.0 * step / 125.0
    # MAX30102 IR dips at systole, so invert. prepare_window inverts back.
    w, status, _ = prepare_window(t_ms, -samples, reference)
    return w


def report(name: str, y: np.ndarray, p: np.ndarray, train_mean: np.ndarray | None) -> dict:
    out = {"mode": name, "n": int(len(y))}
    for i, tgt in enumerate(["SBP", "DBP"]):
        e = p[:, i] - y[:, i]
        r = float(np.corrcoef(y[:, i], p[:, i])[0, 1]) if len(y) > 2 else float("nan")
        res = {
            "ME": e.mean(), "SD": e.std(ddof=1), "MAE": np.abs(e).mean(),
            "RMSE": np.sqrt((e ** 2).mean()), "r": r,
            "within5": np.mean(np.abs(e) <= 5) * 100, "within10": np.mean(np.abs(e) <= 10) * 100,
            "LoA_low": e.mean() - 1.96 * e.std(ddof=1), "LoA_high": e.mean() + 1.96 * e.std(ddof=1),
        }
        if train_mean is not None:
            res["mean_guess_MAE"] = np.abs(y[:, i] - train_mean[i]).mean()
        res["ISO81060_2_crit1"] = "PASS" if abs(res["ME"]) <= 5 and res["SD"] <= 8 else "FAIL"
        out[tgt] = {k: (round(float(v), 2) if not isinstance(v, str) else v) for k, v in res.items()}

    true_band = np.array([news2_sbp_score(v) for v in y[:, 0]])
    pred_band = np.array([news2_sbp_score(v) for v in p[:, 0]])
    low = true_band >= 2  # SBP <= 100
    out["NEWS2_SBP"] = {
        "band_agreement_pct": round(float(np.mean(true_band == pred_band) * 100), 1),
        "patients_with_SBP_le_100": int(low.sum()),
        "sensitivity_SBP_le_100_pct": round(float(np.mean(pred_band[low] >= 2) * 100), 1) if low.any() else None,
        "false_alarm_pct": round(float(np.mean(pred_band[~low] >= 2) * 100), 1) if (~low).any() else None,
    }
    return out


def print_report(r: dict) -> None:
    print(f"\n=== {r['mode']}  (n = {r['n']}) ===")
    for tgt in ("SBP", "DBP"):
        s = r[tgt]
        guess = f"  mean-guess MAE {s['mean_guess_MAE']}" if "mean_guess_MAE" in s else ""
        print(f"{tgt}  ME {s['ME']:+.2f}  SD {s['SD']:.2f}  MAE {s['MAE']:.2f}  r {s['r']:.3f}  "
              f"within 10 {s['within10']:.1f}%  95% LoA [{s['LoA_low']:.1f}, {s['LoA_high']:.1f}]{guess}  ISO {s['ISO81060_2_crit1']}")
    n = r["NEWS2_SBP"]
    sens = "n/a" if n["sensitivity_SBP_le_100_pct"] is None else f"{n['sensitivity_SBP_le_100_pct']}%"
    print(f"NEWS2 SBP band agreement {n['band_agreement_pct']}%  |  SBP<=100 caught {sens} "
          f"of {n['patients_with_SBP_le_100']}  |  false alarms {n['false_alarm_pct']}%")


def bland_altman(y, p, title, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skipping plot")
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, i, tgt in zip(axes, (0, 1), ("SBP", "DBP")):
        mean = (y[:, i] + p[:, i]) / 2
        diff = p[:, i] - y[:, i]
        me, sd = diff.mean(), diff.std(ddof=1)
        ax.scatter(mean, diff, s=6, alpha=0.4)
        for v, ls in ((me, "-"), (me + 1.96 * sd, "--"), (me - 1.96 * sd, "--")):
            ax.axhline(v, color="k", ls=ls, lw=1)
        ax.axhspan(-5, 5, color="green", alpha=0.08)
        ax.set_xlabel(f"Mean of reference and estimate, {tgt} (mmHg)")
        ax.set_ylabel("Estimate minus reference (mmHg)")
        ax.set_title(f"{tgt}  ME {me:+.1f}, SD {sd:.1f}")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"plot  {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--split", default="test", choices=["train", "val", "test"])
    ap.add_argument("--modes", default="patient,window,sensor")
    ap.add_argument("--sensor-fs", type=float, default=25.0)
    ap.add_argument("--max-windows", type=int, default=20, help="per patient, for window and sensor modes")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    ds = Path(args.dataset)
    out = Path(args.out)
    out.mkdir(exist_ok=True, parents=True)
    m = BpModel()
    modes = args.modes.split(",")

    subjects = parse_subjects(ds / f"{args.split}_subjects.txt")
    train_mean = None
    if (ds / "train_subjects.txt").exists() and args.split != "train":
        labs = [np.load(ds / "labels" / f"{s}_labels.npy").mean(axis=0)
                for s in parse_subjects(ds / "train_subjects.txt")
                if (ds / "labels" / f"{s}_labels.npy").exists()]
        if labs:
            train_mean = np.mean(labs, axis=0)

    rows = {k: ([], []) for k in modes}
    detail = []
    used = 0
    for sid in subjects:
        pp, lp = ds / "ppg" / f"{sid}_ppg.npy", ds / "labels" / f"{sid}_labels.npy"
        if not pp.exists() or not lp.exists():
            continue
        ppg, lab = np.load(pp), np.load(lp)
        used += 1
        if "patient" in modes:
            f = np.mean([extract_features(w) for w in ppg], axis=0)
            rows["patient"][0].append(lab.mean(axis=0))
            rows["patient"][1].append(m.model.predict(f.reshape(1, -1))[0])
        idx = np.linspace(0, len(ppg) - 1, min(len(ppg), args.max_windows)).astype(int)
        for j in idx:
            if "window" in modes:
                pr = m.predict_window(ppg[j])
                rows["window"][0].append(lab[j]); rows["window"][1].append(pr)
                detail.append([sid, int(j), "window", *lab[j], *pr])
            if "sensor" in modes:
                w = as_sensor(ppg[j], args.sensor_fs, m.reference)
                if w is None:
                    continue
                pr = m.predict_window(w)
                rows["sensor"][0].append(lab[j]); rows["sensor"][1].append(pr)
                detail.append([sid, int(j), "sensor", *lab[j], *pr])
    print(f"{used} {args.split} subjects loaded from {ds}")

    reports = []
    for k in modes:
        y, p = np.array(rows[k][0]), np.array(rows[k][1])
        if len(y) < 3:
            print(f"{k}: not enough data")
            continue
        r = report(k, y, p, train_mean)
        print_report(r)
        reports.append(r)
        bland_altman(y, p, f"SIREN BP model, {k} mode, {args.split} split", out / f"bland_altman_{k}.png")

    (out / "eval_report.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    with open(out / "eval_predictions.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["subject", "window", "mode", "sbp_true", "dbp_true", "sbp_pred", "dbp_pred"])
        wr.writerows(detail)
    print(f"\nreport  {out / 'eval_report.json'}\npredictions  {out / 'eval_predictions.csv'}")


if __name__ == "__main__":
    main()
