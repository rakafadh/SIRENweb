"""How reliable is "NEWS2 usable"? Repeats the cuff-schedule test with several
random cuff errors and puts 95% confidence intervals on the NEWS2 numbers by
resampling patients (bootstrap). Needs the model saved by vitaldb_train.py.

    python scripts/robustness_check.py
    python scripts/robustness_check.py --seeds 5 --boot 2000
    python scripts/robustness_check.py --final        # SIREN's chosen schedule under realistic delays

--final tests only the chosen SIREN schedule (10 min base + extra cuff on any
5 mmHg / NEWS2 band change, Kalman, cautious score z 0.3) for every
combination of --delays (minutes from "cuff now" to the reading) and
--noises (cuff error SD), with fixed 10 min cuffs at the same delay as the
reference. --cuff-delay applies one delay to the normal full table.

For each schedule it reports, pooled over the seeds:
  * the value and its 95% interval (resampling test patients, not windows,
    because windows from one patient are not independent),
  * the range over seeds (how much the random cuff error matters),
  * P(pass): share of bootstrap samples that meet the criterion.
A schedule is called robust when the whole 95% interval meets every
criterion, borderline when the point value passes but the interval crosses a
limit, and not usable otherwise.
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
from vitaldb_train import load_cases, news2_band, schedule_metrics, schedule_sim, split_cases

ROOT = Path(__file__).resolve().parent.parent


def counts(T, E, PB, subj, hypo_thr):
    """Per-patient numerator/denominator counts for each NEWS2 metric."""
    tb = news2_band(T[:, 0])
    pb = np.asarray(PB)
    hyp = T[:, 0] <= 100
    alert = E[:, 0] <= hypo_thr
    parts = {
        "within1": (np.abs(tb - pb) <= 1, np.ones(len(tb), bool)),
        "miss": ((tb >= 2) & (pb == 0), tb >= 2),
        "hypo": (hyp & alert, hyp),
        "overcall": ((tb == 0) & (pb >= 2), tb == 0),
        "abs_err": (np.abs(E[:, 0] - T[:, 0]), np.ones(len(tb), bool)),
    }
    ids = np.unique(subj)
    pos = np.searchsorted(ids, subj)
    out = {}
    for k, (num, den) in parts.items():
        n = np.bincount(pos, weights=num.astype(float), minlength=len(ids))
        d = np.bincount(pos, weights=den.astype(float), minlength=len(ids))
        out[k] = (n, d)
    return ids, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"))
    ap.add_argument("--model", default=str(ROOT / "model" / "bp_calibrated_vitaldb.joblib"))
    ap.add_argument("--settings", default=str(ROOT / "model" / "alert_settings_vitaldb.json"))
    ap.add_argument("--seeds", type=int, default=5, help="random cuff-error repeats")
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap resamples of test patients")
    ap.add_argument("--cuff-noise", type=float, default=6.0)
    ap.add_argument("--smart-trigger", type=float, default=8.0, help="mmHg change that triggers an extra cuff")
    ap.add_argument("--cuff-delay", type=float, default=0.0,
                    help="minutes from 'cuff now' to the reading, for every schedule in the full table")
    ap.add_argument("--final", action="store_true", help="only the chosen SIREN schedule over --delays x --noises")
    ap.add_argument("--delays", default="0,0.75,1.5,3", help="--final: delays in minutes, comma separated")
    ap.add_argument("--noises", default="6,8", help="--final: cuff error SDs, comma separated")
    ap.add_argument("--final-trigger", type=float, default=5.0)
    ap.add_argument("--final-z", type=float, default=0.3)
    ap.add_argument("--displays", default="live",
                    help="--final: what is shown while a cuff is pending, comma separated from live, hold_estimate, hold_cuff")
    ap.add_argument("--confirm-mmhg", type=float, default=10.0,
                    help="confirmatory cuff 2 min later when a cuff differs from the estimate by more than this")
    ap.add_argument("--news2-within1", type=float, default=95.0)
    ap.add_argument("--news2-max-miss", type=float, default=5.0)
    ap.add_argument("--news2-min-hypo-sens", type=float, default=80.0)
    ap.add_argument("--seed", type=int, default=42, help="patient split seed (must match vitaldb_train.py)")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()
    T0 = time.time()

    bundle = joblib.load(args.model)
    model = bundle["model"]
    hl = bundle.get("anchor_half_life_min") or np.inf
    st = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    hypo_thr = st.get("hypotension_alert_sbp", 100.0)
    kcfg = KalmanConfig(**st["kalman"]) if "kalman" in st else None
    z = float(st.get("news2_score_z", 0.3))
    data = split_cases(load_cases(Path(args.cache)), args.seed)
    test = data["test"]
    print(f"{len(test)} test cases; hypotension alert at estimate <= {hypo_thr}; Kalman z {z:g}", flush=True)
    crit = {"within1": args.news2_within1, "max_miss": args.news2_max_miss, "min_hypo_sens": args.news2_min_hypo_sens}

    def tune_z(base, trig, unc):
        """z for 10 min + smart triggers + Kalman, chosen on VALIDATION (3 repeats): smallest z that passes,
        else the fewest dangerous misses while keeping within one band, else 0."""
        res = {}
        for zk in (0.0, 0.3, 0.5, 0.7):
            Ts, Es, Ps = [], [], []
            for sd in range(3):
                T, E, _, _, x = schedule_sim(data["val"], model, base, trig, args.cuff_noise,
                                             np.random.default_rng(200 + sd), hl, kalman=kcfg, score_z=zk,
                                             unc_trigger=unc, extra=True)
                Ts.append(T); Es.append(E); Ps.append(x["band"])
            res[zk] = schedule_metrics(np.concatenate(Ts), np.concatenate(Es), hypo_thr, crit, PB=np.concatenate(Ps))
            print(f"    val z {zk:g}{' + uncertainty cuff' if unc else ''}: within 1 band {res[zk]['band_within1']:.1f}%  "
                  f"dangerous miss {res[zk]['miss']:.1f}%", flush=True)
        ok = [zk for zk, r in res.items() if r["news2_usable"]]
        keep = [zk for zk, r in res.items() if r["checks"]["within1"]]
        return min(ok) if ok else (min(keep, key=lambda zk: res[zk]["miss"]) if keep else 0.0)

    schedules = [("Fixed every 3 min", 3, {}),
                 ("Fixed every 5 min", 5, {}),
                 ("Fixed every 5 min, score 3 mmHg lower", 5, {"fixed_margin": 3.0}),
                 ("Fixed every 10 min", 10, {}),
                 (f"10 min + extra cuff on any {args.smart_trigger:g} mmHg change or NEWS2 band change", 10,
                  {"trig": args.smart_trigger}),
                 (f"10 min + extra cuff on {args.smart_trigger:g} mmHg drop or worse NEWS2 band", 10,
                  {"trig": args.smart_trigger, "drop_only": True}),
                 (f"Fixed every 5 min + confirmatory cuff (> {args.confirm_mmhg:g} mmHg off)", 5,
                  {"confirm_mmhg": args.confirm_mmhg}),
                 (f"10 min + any {args.smart_trigger:g} mmHg / band change + confirmatory cuff "
                  f"(> {args.confirm_mmhg:g} mmHg off)", 10, {"trig": args.smart_trigger, "confirm_mmhg": args.confirm_mmhg})]
    if args.final:
        if kcfg is None:
            raise SystemExit("--final needs the Kalman settings (run vitaldb_train.py without --no-kalman)")
        schedules = []
        for nz in [float(v) for v in args.noises.split(",")]:
            for dl in [float(v) for v in args.delays.split(",")]:
                for disp in args.displays.split(","):
                    tag = "" if disp == "live" else f", {disp.replace('_', ' ')} while waiting"
                    schedules.append((f"SIREN schedule, cuff SD {nz:g}, delay {dl:g} min{tag}", 10,
                                      {"trig": args.final_trigger, "kalman": kcfg, "score_z": args.final_z,
                                       "noise": nz, "cuff_delay_min": dl, "pending_display": disp}))
                schedules.append((f"Fixed every 10 min, cuff SD {nz:g}, delay {dl:g} min", 10,
                                  {"noise": nz, "cuff_delay_min": dl}))
    elif kcfg is not None:
        schedules += [(f"Fixed every 3 min, Kalman z={z:g}", 3, {"kalman": kcfg, "score_z": z}),
                      (f"5 min + uncertainty cuff, Kalman z={z:g}", 5, {"kalman": kcfg, "score_z": z, "unc_trigger": True}),
                      (f"10 min + uncertainty cuff, Kalman z={z:g}", 10, {"kalman": kcfg, "score_z": z, "unc_trigger": True})]
        print("tuning z on validation for 10 min + any change + Kalman...", flush=True)
        for unc in (False, True):
            zt = tune_z(10, args.smart_trigger, unc)
            schedules.append((f"10 min + any {args.smart_trigger:g} mmHg / band change, Kalman z={zt:g} (val)"
                              + (" + uncertainty cuff" if unc else ""), 10,
                              {"trig": args.smart_trigger, "kalman": kcfg, "score_z": zt, "unc_trigger": unc}))

    rng_b = np.random.default_rng(0)
    rows = []
    for label, base, kw in schedules:
        per_seed, pooled, ncs = [], None, []
        kw = dict(kw)
        trig = kw.pop("trig", None)
        noise = kw.pop("noise", args.cuff_noise)
        kw.setdefault("cuff_delay_min", args.cuff_delay)
        for sd in range(args.seeds):
            T, E, _, nc, x = schedule_sim(test, model, base, trig, noise, np.random.default_rng(100 + sd),
                                          hl, extra=True, **kw)
            ids, cnt = counts(T, E, x["band"], x["subject"], hypo_thr)
            per_seed.append({k: 100 * n.sum() / d.sum() if k != "abs_err" else n.sum() / d.sum()
                             for k, (n, d) in cnt.items()})
            ncs.append(nc.mean())
            if pooled is None:
                pooled = {k: [n.copy(), d.copy()] for k, (n, d) in cnt.items()}
            else:
                for k, (n, d) in cnt.items():
                    pooled[k][0] += n
                    pooled[k][1] += d
        n_pat = len(ids)
        idx = rng_b.integers(0, n_pat, size=(args.boot, n_pat))
        res = {}
        for k, (n, d) in pooled.items():
            scale = 1.0 if k == "abs_err" else 100.0
            point = scale * n.sum() / d.sum()
            bs = scale * n[idx].sum(axis=1) / np.maximum(d[idx].sum(axis=1), 1e-9)
            res[k] = (point, np.percentile(bs, 2.5), np.percentile(bs, 97.5), bs,
                      min(s_[k] for s_ in per_seed), max(s_[k] for s_ in per_seed))
        lim = {"within1": (">=", args.news2_within1), "miss": ("<=", args.news2_max_miss),
               "hypo": (">=", args.news2_min_hypo_sens)}
        p_pass, robust, point_ok = {}, True, True
        for k, (op, v) in lim.items():
            point, lo, hi, bs = res[k][:4]
            ok = bs >= v if op == ">=" else bs <= v
            p_pass[k] = float(ok.mean() * 100)
            point_ok &= point >= v if op == ">=" else point <= v
            robust &= (lo >= v) if op == ">=" else (hi <= v)
        all_pass = float(np.mean(np.all([(res[k][3] >= v) if op == ">=" else (res[k][3] <= v)
                                         for k, (op, v) in lim.items()], axis=0)) * 100)
        verdict = "ROBUST" if robust else "BORDERLINE" if point_ok else "NOT USABLE"
        rows.append((label, float(np.mean(ncs)), res, p_pass, all_pass, verdict, n_pat))
        r = res["miss"]
        print(f"  {label:<45} cuffs/15 min {np.mean(ncs):.2f}  dangerous miss {r[0]:.1f}% (95% CI {r[1]:.1f}-{r[2]:.1f}, "
              f"seeds {r[4]:.1f}-{r[5]:.1f})  P(all pass) {all_pass:.0f}%  {verdict}  [{time.time() - T0:.0f} s]", flush=True)

    f = lambda r, d=1: f"{r[0]:.{d}f} ({r[1]:.{d}f}-{r[2]:.{d}f})"  # noqa: E731
    md = [f"# NEWS2 usability: robustness check\n\n{rows[0][6] if rows else 0} test patients, {args.seeds} random cuff-error "
          f"repeats (SD {args.cuff_noise:g} mmHg unless the row says otherwise; cuff delay "
          f"{args.cuff_delay:g} min unless the row says otherwise), {args.boot} bootstrap resamples of patients. "
          "Values pooled over repeats, "
          "with 95% confidence intervals in brackets. Criteria (proposed SIREN, not an official standard): within one band "
          f">= {args.news2_within1:g}%, dangerous miss <= {args.news2_max_miss:g}%, hypotension alert sensitivity "
          f">= {args.news2_min_hypo_sens:g}%.\n\n"
          "ROBUST = the whole 95% interval meets every criterion; BORDERLINE = the point value passes but an interval "
          "crosses a limit; P(all pass) = share of bootstrap samples meeting all three.\n\n"
          "| Schedule | Cuffs per 15 min | SBP MAE | Within one band | Dangerous miss | Dangerous miss over repeats "
          "| Hypotension alert sens | Over-triage | P(all pass) | Verdict |\n|---|---|---|---|---|---|---|---|---|---|\n"]
    for label, ncm, res, p_pass, all_pass, verdict, _ in rows:
        md.append(f"| {label} | {ncm:.1f} | {f(res['abs_err'], 2)} | {f(res['within1'])}% | {f(res['miss'])}% "
                  f"| {res['miss'][4]:.1f}-{res['miss'][5]:.1f}% | {f(res['hypo'])}% | {f(res['overcall'])}% "
                  f"| {all_pass:.0f}% | **{verdict}** |\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "robustness_check.md").write_text("".join(md), encoding="utf-8")
    print("".join(md))
    print(f"written {out / 'robustness_check.md'}  [{time.time() - T0:.0f} s]")


if __name__ == "__main__":
    main()
