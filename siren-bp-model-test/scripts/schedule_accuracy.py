"""Accuracy of what the crew SEES under each cuff schedule (ISO 81060-2 and BHS).

    python scripts/schedule_accuracy.py

The single-cuff table in vitaldb_comparison.md scores the model with one cuff
and estimates up to 15 min later. In SIREN the estimate shown is always based
on the latest cuff(s), with extra cuffs when it changes, so the error of the
displayed value is smaller. This script reports, for the same test patients,
SBP and DBP mean error, SD, MAE and the share within 5 / 10 / 15 mmHg of the
displayed value under:
  - the SIREN schedule (10 min + extra cuff on any 5 mmHg / NEWS2 band change,
    Kalman, z 0.3), with the cuff arriving at once and after 0.75 min,
  - fixed cuffs every 10, 5 and 3 min (model between cuffs),
  - holding the last cuff every 10 min (no model).
Cuff error SD 6 mmHg, 5 repeats pooled. ISO 81060-2 criterion 1: |ME| <= 5 and
SD <= 8 mmHg. BHS: A >= 60/85/95%, B >= 50/75/90%, C >= 40/65/85% within
5/10/15 mmHg, else D. Writes results/schedule_accuracy.md.
"""
import _path  # noqa: F401
import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import joblib
import numpy as np

from bp_model.kalman import KalmanConfig
from vitaldb_train import load_cases, schedule_sim, split_cases

ROOT = Path(__file__).resolve().parent.parent


def bhs(w5, w10, w15):
    for g, (a, b, c) in (("A", (60, 85, 95)), ("B", (50, 75, 90)), ("C", (40, 65, 85))):
        if w5 >= a and w10 >= b and w15 >= c:
            return g
    return "D"


def stats(err):
    a = np.abs(err)
    w = [float(np.mean(a <= k) * 100) for k in (5, 10, 15)]
    me, sd = float(err.mean()), float(err.std(ddof=1))
    return {"ME": me, "SD": sd, "MAE": float(a.mean()), "w": w, "BHS": bhs(*w),
            "ISO": "PASS" if abs(me) <= 5 and sd <= 8 else "FAIL"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"))
    ap.add_argument("--model", default=str(ROOT / "model" / "bp_calibrated_vitaldb.joblib"))
    ap.add_argument("--settings", default=str(ROOT / "model" / "alert_settings_vitaldb.json"))
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--cuff-noise", type=float, default=6.0)
    ap.add_argument("--trigger", type=float, default=5.0)
    ap.add_argument("--z", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42, help="patient split seed (must match vitaldb_train.py)")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()
    T0 = time.time()

    bundle = joblib.load(args.model)
    model = bundle["model"]
    hl = bundle.get("anchor_half_life_min") or np.inf
    st = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    kcfg = KalmanConfig(**st["kalman"]) if "kalman" in st else None
    test = split_cases(load_cases(Path(args.cache)), args.seed)["test"]
    print(f"{len(test)} test cases", flush=True)

    siren = {"trig": args.trigger, "kalman": kcfg, "score_z": args.z} if kcfg else {"trig": args.trigger}
    schedules = [
        ("SIREN schedule, cuff at once", 10, dict(siren)),
        ("SIREN schedule, cuff after 0.75 min (estimate held while waiting)", 10,
         dict(siren, cuff_delay_min=0.75, pending_display="hold_estimate")),
        ("Fixed every 10 min, model between cuffs", 10, {}),
        ("Fixed every 5 min, model between cuffs", 5, {}),
        ("Fixed every 3 min, model between cuffs", 3, {}),
        ("Fixed every 10 min, hold last cuff (no model)", 10, {"hold": True}),
    ]
    rows = []
    for label, base, kw in schedules:
        kw = dict(kw)
        trig = kw.pop("trig", None)
        hold = kw.pop("hold", False)
        Ts, Es, ncs = [], [], []
        for sd in range(args.seeds):
            T, E, H, nc = schedule_sim(test, model, base, trig, args.cuff_noise, np.random.default_rng(100 + sd), hl, **kw)
            Ts.append(T); Es.append(H if hold else E); ncs.append(nc.mean())
        T, E = np.concatenate(Ts), np.concatenate(Es)
        r = (label, float(np.mean(ncs)), stats(E[:, 0] - T[:, 0]), stats(E[:, 1] - T[:, 1]))
        rows.append(r)
        print(f"  {label:<70} cuffs/15 min {r[1]:.1f}  SBP ME {r[2]['ME']:+.2f} SD {r[2]['SD']:.2f} "
              f"BHS {r[2]['BHS']} ISO {r[2]['ISO']}  |  DBP ME {r[3]['ME']:+.2f} SD {r[3]['SD']:.2f} "
              f"BHS {r[3]['BHS']} ISO {r[3]['ISO']}  [{time.time() - T0:.0f} s]", flush=True)

    md = [f"# Accuracy of the displayed BP under each cuff schedule\n\n{len(test)} test patients, cuff error SD "
          f"{args.cuff_noise:g} mmHg, {args.seeds} repeats pooled, estimates over 15 min. ISO 81060-2 criterion 1: "
          "|mean error| <= 5 and SD <= 8 mmHg. BHS grade from % within 5 / 10 / 15 mmHg (A >= 60/85/95, "
          "B >= 50/75/90, C >= 40/65/85).\n\n"
          "| Schedule | Cuffs / 15 min | SBP ME | SBP SD | SBP MAE | SBP within 5/10/15 | SBP BHS | SBP ISO "
          "| DBP ME | DBP SD | DBP MAE | DBP within 5/10/15 | DBP BHS | DBP ISO |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"]
    for label, nc, s, d in rows:
        md.append(f"| {label} | {nc:.1f} | {s['ME']:+.2f} | {s['SD']:.2f} | {s['MAE']:.2f} | "
                  f"{s['w'][0]:.0f}/{s['w'][1]:.0f}/{s['w'][2]:.0f}% | {s['BHS']} | {s['ISO']} | {d['ME']:+.2f} | "
                  f"{d['SD']:.2f} | {d['MAE']:.2f} | {d['w'][0]:.0f}/{d['w'][1]:.0f}/{d['w'][2]:.0f}% | {d['BHS']} | "
                  f"{d['ISO']} |\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schedule_accuracy.md").write_text("".join(md), encoding="utf-8")
    print("".join(md))
    print(f"written {out / 'schedule_accuracy.md'}  [{time.time() - T0:.0f} s]")


if __name__ == "__main__":
    main()
