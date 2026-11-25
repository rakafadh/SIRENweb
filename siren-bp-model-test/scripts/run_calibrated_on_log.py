"""Run the CALIBRATED model on a real SIREN serial log (W lines).

    python scripts/run_calibrated_on_log.py my_log.txt --cuff 118/76
    python scripts/run_calibrated_on_log.py my_log.txt --cuff 118/76 --cuff-at 35 --check 160/95@300

--cuff     the reading used for calibration
--cuff-at  seconds into the log when that reading was taken (default: first 30 s)
--check    later cuff readings to compare against, SBP/DBP@seconds, comma separated

Retrain first (scripts/retrain_calibrated.py) so model/bp_calibrated.joblib exists.
"""
import _path  # noqa: F401
import argparse
import csv
from pathlib import Path

import numpy as np

from bp_model import parse_waveform_log
from bp_model.calibrated import MODEL_DIR, CalibratedBpModel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--cuff", required=True)
    ap.add_argument("--cuff-at", type=float, default=30.0)
    ap.add_argument("--check", default="")
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--use-checks", action="store_true")
    ap.add_argument("--model", choices=["mimic", "vitaldb"], default="mimic",
                    help="which trained model to use (vitaldb needs scripts/vitaldb_train.py first)")
    ap.add_argument("--out", default="results/log_calibrated.csv")
    args = ap.parse_args()

    t, ir = parse_waveform_log(args.log)
    if t.size == 0:
        raise SystemExit("No W lines found in the log.")
    t0 = t[0]
    if args.model == "vitaldb":
        m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", MODEL_DIR / "alert_settings_vitaldb.json")
    else:
        m = CalibratedBpModel()
    s, d = (float(v) for v in args.cuff.split("/"))
    sel = t <= t0 + max(args.cuff_at, 30) * 1000
    ok, why = m.calibrate(t[sel], ir[sel], s, d)
    print(f"calibration with cuff {args.cuff} at {args.cuff_at:.0f} s -> {why}  (model: {m.name})")
    if not ok:
        raise SystemExit("Calibration rejected. Record a steadier 30 s with the finger still.")

    checks = []
    for item in filter(None, args.check.split(",")):
        bp, at = item.split("@")
        cs, cd = (float(v) for v in bp.split("/"))
        checks.append((t0 + float(at) * 1000, cs, cd))
    pending = sorted(checks) if args.use_checks else []

    rows, end = [], t[sel][-1] + args.step * 1000
    while end <= t[-1]:
        win = (t > end - 31000) & (t <= end)
        while pending and pending[0][0] <= end:
            _, cs, cd = pending.pop(0)
            ok2, why2 = m.calibrate(t[win], ir[win], cs, cd)
            print(f"extra cuff {cs:.0f}/{cd:.0f} at {(end - t0) / 1000:.0f} s -> {why2}")
        r = m.estimate(t[win], ir[win])
        rows.append({"t_s": round((end - t0) / 1000, 1), **r})
        end += args.step * 1000
    keys = ["t_s", "status", "sbp", "dbp", "delta_sbp", "delta_dbp", "minutes_since_cuff", "cuffs_used",
            "recalibrate", "recalibrate_reasons"]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    okr = [r for r in rows if r["status"] in ("OK", "STALE")]
    print(f"{len(rows)} windows, {len(okr)} with an estimate")
    if okr:
        sb = np.array([r["sbp"] for r in okr])
        print(f"SBP {sb.mean():.1f} (range {sb.min():.1f}-{sb.max():.1f})")
    for item in filter(None, args.check.split(",")):
        bp, at = item.split("@")
        cs, cd = (float(v) for v in bp.split("/"))
        # the estimate just BEFORE the check, so a reused check is not compared with itself
        before = [r for r in okr if r["t_s"] < float(at)]
        near = before[-1] if before else None
        if near:
            print(f"check at {at} s: cuff {cs:.0f}/{cd:.0f}, model {near['sbp']}/{near['dbp']} "
                  f"(error {near['sbp'] - cs:+.1f}/{near['dbp'] - cd:+.1f})")
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
