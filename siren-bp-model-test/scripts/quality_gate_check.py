"""Choose and check the rescue quality gate on VitalDB (bp_model/quality.py).

    python scripts/quality_gate_check.py            # choose on validation, check on test
    python scripts/quality_gate_check.py --write    # also switch it on in the settings file

The strict gate throws away a whole 30 s window when a few beats are spoilt.
The rescue gate also keeps windows in which most beats are clean. More windows
means more estimates and more successful calibrations, but only if the error
does not grow. This script:
  1. replays the SIREN schedule (10 min + any 5 mmHg / NEWS2 band change,
     Kalman, z 0.3, cuff SD 6) on the VALIDATION patients for a grid of rescue
     thresholds, and keeps the setting that adds the most estimates while the
     dangerous miss, the SBP error SD and "within one band" stay within a small
     margin of the strict gate,
  2. reports strict vs chosen on the TEST patients (5 cuff-error repeats):
     estimates shown, cuffs used, dangerous miss, within one band, hypotension
     alert, SBP / DBP SD and MAE, BHS,
  3. with --write, saves the setting as "quality" in alert_settings_vitaldb.json
     (only if it also holds on test), which the live model then uses.
Writes results/quality_gate_check.md.
"""
import _path  # noqa: F401
import argparse
import copy
import json
import os
import time
from pathlib import Path

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import joblib
import numpy as np

import vitaldb_train as vt
from bp_model.kalman import KalmanConfig
from bp_model.quality import QualityGate, effective_sqi

ROOT = Path(__file__).resolve().parent.parent


def gated(cases, g):
    out = []
    for c in cases:
        d = dict(c)
        d["sqi"] = effective_sqi(c["F"], g)
        d["Frec"] = vt.recent_features(d, 0.80)
        out.append(d)
    vt._PAIR_CACHE.clear()
    return out


def run(cases, model, hl, kcfg, args, hypo_thr, seeds):
    Ts, Es, Ps, ncs = [], [], [], []
    kw = {"kalman": kcfg, "score_z": args.z} if kcfg else {}
    for sd in range(seeds):
        T, E, _, nc, x = vt.schedule_sim(cases, model, 10, args.trigger, args.cuff_noise,
                                         np.random.default_rng(100 + sd), hl, extra=True, **kw)
        Ts.append(T); Es.append(E); Ps.append(x["band"]); ncs.append(nc.mean())
    T, E, P = np.concatenate(Ts), np.concatenate(Es), np.concatenate(Ps)
    m = vt.schedule_metrics(T, E, hypo_thr, PB=P)
    d = E[:, 1] - T[:, 1]
    m.update({"n": len(T) / seeds, "cuffs": float(np.mean(ncs)), "DBP_SD": float(d.std(ddof=1))})
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"))
    ap.add_argument("--model", default=str(ROOT / "model" / "bp_calibrated_vitaldb.joblib"))
    ap.add_argument("--settings", default=str(ROOT / "model" / "alert_settings_vitaldb.json"))
    ap.add_argument("--seeds", type=int, default=5, help="cuff-error repeats on test")
    ap.add_argument("--val-seeds", type=int, default=2)
    ap.add_argument("--cuff-noise", type=float, default=6.0)
    ap.add_argument("--trigger", type=float, default=5.0)
    ap.add_argument("--z", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--margin-miss", type=float, default=0.3, help="allowed rise in dangerous miss (points)")
    ap.add_argument("--margin-sd", type=float, default=0.3, help="allowed rise in SBP error SD (mmHg)")
    ap.add_argument("--margin-band", type=float, default=0.5, help="allowed drop in within one band (points)")
    ap.add_argument("--write", action="store_true", help="save the chosen gate in the settings file")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()
    T0 = time.time()

    bundle = joblib.load(args.model)
    model = bundle["model"]
    hl = bundle.get("anchor_half_life_min") or np.inf
    st = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    kcfg = KalmanConfig(**st["kalman"]) if "kalman" in st else None
    hypo_thr = st.get("hypotension_alert_sbp", 100.0)
    sp = vt.split_cases(vt.load_cases(Path(args.cache)), args.seed)
    print(f"{len(sp['val'])} validation and {len(sp['test'])} test patients", flush=True)

    strict = QualityGate()
    base_v = run(gated(sp["val"], strict), model, hl, kcfg, args, hypo_thr, args.val_seeds)
    print(f"  strict (validation): estimates {base_v['n']:.0f}  miss {base_v['miss']:.2f}%  "
          f"SBP SD {base_v['SD']:.2f}  within1 {base_v['band_within1']:.2f}%  [{time.time() - T0:.0f} s]", flush=True)

    grid = [QualityGate(rescue=True, rescue_est_sqi=es, rescue_est_frac=ef, rescue_cal_sqi=cs,
                        rescue_cal_frac=min(ef + 0.10, 0.95))
            for es in (0.55, 0.60, 0.65, 0.70) for ef in (0.70, 0.75, 0.80) for cs in (0.75, 0.80, 0.85)]
    best, best_m = None, None
    for g in grid:
        m = run(gated(sp["val"], g), model, hl, kcfg, args, hypo_thr, args.val_seeds)
        ok = (m["miss"] <= base_v["miss"] + args.margin_miss and m["SD"] <= base_v["SD"] + args.margin_sd
              and m["band_within1"] >= base_v["band_within1"] - args.margin_band)
        print(f"  est {g.rescue_est_sqi:.2f}/{g.rescue_est_frac:.2f} cal {g.rescue_cal_sqi:.2f}/{g.rescue_cal_frac:.2f}: "
              f"estimates {m['n']:.0f} (+{(m['n'] / base_v['n'] - 1) * 100:.1f}%)  miss {m['miss']:.2f}%  "
              f"SD {m['SD']:.2f}  within1 {m['band_within1']:.2f}%  {'ok' if ok else '-'}  [{time.time() - T0:.0f} s]",
              flush=True)
        if ok and (best_m is None or m["n"] > best_m["n"]):
            best, best_m = g, m

    rows = [("Strict gate (as validated)", strict)]
    if best is not None:
        rows.append((f"Rescue gate: estimate {best.rescue_est_sqi:.2f} with >= {best.rescue_est_frac:.0%} clean beats, "
                     f"calibrate {best.rescue_cal_sqi:.2f} with >= {best.rescue_cal_frac:.0%}", best))
    res = []
    for label, g in rows:
        m = run(gated(sp["test"], g), model, hl, kcfg, args, hypo_thr, args.seeds)
        res.append((label, g, m))
        print(f"  TEST {label}: estimates {m['n']:.0f}  cuffs {m['cuffs']:.2f}  miss {m['miss']:.2f}%  "
              f"within1 {m['band_within1']:.2f}%  hypo {m['hypo_sens']:.1f}%  SBP SD {m['SD']:.2f}  "
              f"DBP SD {m['DBP_SD']:.2f}  BHS {m['BHS']}  [{time.time() - T0:.0f} s]", flush=True)

    holds = False
    if best is not None:
        s, r = res[0][2], res[1][2]
        holds = (r["miss"] <= max(s["miss"] + args.margin_miss, 0) and r["miss"] <= 5.0
                 and r["SD"] <= s["SD"] + args.margin_sd and r["band_within1"] >= 95.0)
    md = [f"# Quality gate check (VitalDB, SIREN schedule, cuff SD {args.cuff_noise:g})\n\n"
          f"Chosen on {len(sp['val'])} validation patients ({args.val_seeds} repeats), checked on "
          f"{len(sp['test'])} test patients ({args.seeds} repeats). Estimates = windows with a displayed value.\n\n"
          "| Gate | Estimates | Cuffs / 15 min | Dangerous miss | Within one band | Hypotension alert | "
          "SBP SD | SBP MAE | DBP SD | DBP MAE | SBP BHS |\n|---|---|---|---|---|---|---|---|---|---|---|\n"]
    base_n = res[0][2]["n"]
    for label, g, m in res:
        md.append(f"| {label} | {m['n']:.0f} ({(m['n'] / base_n - 1) * 100:+.1f}%) | {m['cuffs']:.2f} | "
                  f"{m['miss']:.2f}% | {m['band_within1']:.2f}% | {m['hypo_sens']:.1f}% | {m['SD']:.2f} | "
                  f"{m['MAE']:.2f} | {m['DBP_SD']:.2f} | {m['DBP_MAE']:.2f} | {m['BHS']} |\n")
    if best is None:
        md.append("\nNo rescue setting kept the error within the margins on validation: keep the strict gate.\n")
    else:
        md.append(f"\nHolds on test: **{'yes' if holds else 'no'}**.\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "quality_gate_check.md").write_text("".join(md), encoding="utf-8")
    print("".join(md))
    if args.write:
        if best is not None and holds:
            st["quality"] = {"quality_rescue": True, "rescue_est_sqi": best.rescue_est_sqi,
                             "rescue_est_frac": best.rescue_est_frac, "rescue_cal_sqi": best.rescue_cal_sqi,
                             "rescue_cal_frac": best.rescue_cal_frac}
            Path(args.settings).write_text(json.dumps(st, indent=2), encoding="utf-8")
            print(f"rescue gate switched on in {args.settings}")
        else:
            print("not written: the rescue gate did not hold on test, the strict gate stays")
    print(f"[{time.time() - T0:.0f} s]")


if __name__ == "__main__":
    main()
