"""Download PLETH at 100 Hz again for VitalDB cases you already have and save
respiratory-variation features (bp_model/respvar.py) next to the PPG features.

    python scripts/vitaldb_resp_download.py --n-cases 300
    python scripts/vitaldb_pat_train.py --cache vitaldb_resp_cache --extra-label "PPG + breathing variation" --report vitaldb_resp_comparison.md

Same case choice and patient split as vitaldb_pat_download.py (70/15/15 of the
existing split). Lighter than the ECG download: PLETH only, about 10-20 MB per
case on the server side; resumable. Stores per 30 s window F (26 PPG
features), P (5 breathing-variation features), L (arterial SBP/DBP), cuffs.
VitalDB patients are ventilated, where pleth variability is best studied;
in spontaneously breathing ambulance patients it also reflects breathing
effort, so a positive result here still needs checking on SIREN recordings.
"""
import _path  # noqa: F401
import argparse
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from scipy.signal import decimate

from bp_model.features_v2 import FEATURE_VERSION, extract
from bp_model.respvar import N_RESP, RESP_NAMES, resp_features
from vitaldb_download import window_label
from vitaldb_pat_download import NUMS

ROOT = Path(__file__).resolve().parent.parent
FS = 100.0
WIN_S = 30.0
RESP_VERSION = "resp100_v1"


def process(ppg: np.ndarray, num: np.ndarray, fs: float = FS, win_s: float = WIN_S) -> dict:
    n_win = int(min(len(ppg) / fs, len(num)) // win_s)
    step = int(fs / 25.0)
    T, F, P, L = [], [], [], []
    for w in range(n_win):
        a, b = int(w * fs * win_s), int((w + 1) * fs * win_s)
        lab = window_label(num[int(w * win_s):int((w + 1) * win_s), 0], num[int(w * win_s):int((w + 1) * win_s), 1])
        if lab is None:
            continue
        seg = ppg[a:b]
        if np.isnan(seg).mean() > 0.02:
            continue
        seg = np.where(np.isnan(seg), np.nanmedian(seg), seg)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            f = extract(decimate(seg, step, ftype="fir", zero_phase=True))
            v = resp_features(seg, fs)
        if np.isnan(f).all():
            continue
        T.append(b / fs); F.append(f); P.append(v); L.append(lab)
    ns, nd = num[:, 2], num[:, 3]
    k = np.where(np.isfinite(ns) & np.isfinite(nd))[0]
    if k.size:
        new_val = np.r_[True, (np.diff(ns[k]) != 0) | (np.diff(nd[k]) != 0)]
        k = k[new_val | np.r_[True, np.diff(k) > 20]]
    nibp = np.column_stack([k * 1.0, ns[k], nd[k]]) if k.size else np.zeros((0, 3))
    nibp = nibp[(nibp[:, 1] > 40) & (nibp[:, 1] < 260)] if len(nibp) else nibp
    return {"t": np.array(T), "F": np.array(F).reshape(-1, 26), "P": np.array(P).reshape(-1, N_RESP),
            "L": np.array(L).reshape(-1, 2), "nibp": nibp}


def fetch(caseid: int, subjectid: int, out_dir: Path) -> str:
    import vitaldb
    out = out_dir / f"{caseid}.npz"
    if out.exists():
        return "cached"
    wave = vitaldb.load_case(caseid, ["SNUADC/PLETH"], 1 / FS)
    if wave is None or len(wave) == 0:
        np.savez_compressed(out, empty=True, version=FEATURE_VERSION)
        return "no PLETH, skipped"
    num = vitaldb.load_case(caseid, NUMS, 1.0)
    r = process(np.asarray(wave, dtype=np.float64)[:, 0], np.asarray(num, dtype=np.float64))
    np.savez_compressed(out, version=FEATURE_VERSION, extra_version=RESP_VERSION, extra_names=np.array(RESP_NAMES),
                        caseid=caseid, subjectid=subjectid, t=r["t"], F=r["F"], P=r["P"], L=r["L"], nibp=r["nibp"])
    good = np.isfinite(r["P"][:, 0]).mean() * 100 if len(r["P"]) else 0
    return f"{len(r['t'])} windows, breathing variation found in {good:.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-cases", type=int, default=300)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"), help="existing PPG cache (for case ids)")
    ap.add_argument("--out", default=str(ROOT / "vitaldb_resp_cache"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    from vitaldb_train import load_cases, split_cases
    data = split_cases(load_cases(Path(args.cache)), args.seed)
    rng = np.random.default_rng(args.seed)
    chosen = []
    for part, frac in (("train", 0.70), ("val", 0.15), ("test", 0.15)):
        cs = data[part]
        k = min(len(cs), max(1, int(round(args.n_cases * frac))))
        chosen += [(cs[i]["caseid"], cs[i]["subject"]) for i in rng.choice(len(cs), k, replace=False)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    todo = [c for c in chosen if not (out / f"{c[0]}.npz").exists()]
    print(f"{len(chosen)} cases chosen, {len(chosen) - len(todo)} already done, {len(todo)} to download", flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(fetch, cid, sid, out): cid for cid, sid in todo}
        for fut in as_completed(futs):
            done += 1
            try:
                msg = fut.result()
            except Exception as exc:
                msg = f"failed ({exc.__class__.__name__}: {exc})"
            eta = (time.time() - t0) / done * (len(todo) - done) / 60
            print(f"[{done}/{len(todo)}] case {futs[fut]}: {msg}   ~{eta:.0f} min left", flush=True)
    print(f"done. cache in {out}")


if __name__ == "__main__":
    main()
