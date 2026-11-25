"""Download ECG lead II + PLETH at 250 Hz for VitalDB cases you already have,
and save pulse arrival time (PAT) features next to the usual PPG features.

    python scripts/vitaldb_pat_download.py --n-cases 150
    python scripts/vitaldb_pat_download.py --n-cases 150 --threads 2

Cases are taken from vitaldb_cache/ (made by vitaldb_download.py) and
sampled 70/15/15 from the same patient split as vitaldb_train.py, so the
test patients stay test patients. For every case it downloads
  SNUADC/ECG_II and SNUADC/PLETH at 250 Hz (same ADC, so on one clock),
  Solar8000 ART_SBP/ART_DBP and NIBP_SBP/NIBP_DBP at 1 Hz,
cuts back-to-back 30 s windows and stores per window
  F: the 26 PPG features at 25 Hz (as before), P: 6 PAT features
  (bp_model/pat.py), L: arterial SBP/DBP label, plus the cuff readings,
in vitaldb_pat_cache/<caseid>.npz. Raw waveforms are not kept.

Network: about 20-60 MB per case (ECG and PLETH are 500 Hz on the server).
150 cases is a few GB and an hour or two; interrupted runs resume. Cases
without ECG_II are skipped and marked.
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
from bp_model.pat import N_PAT, pat_features
from vitaldb_download import window_label

ROOT = Path(__file__).resolve().parent.parent
FS = 250.0
WIN_S = 30.0
WAVES = ["SNUADC/ECG_II", "SNUADC/PLETH"]
NUMS = ["Solar8000/ART_SBP", "Solar8000/ART_DBP", "Solar8000/NIBP_SBP", "Solar8000/NIBP_DBP"]
PAT_VERSION = "pat250_v1"


def process(wave: np.ndarray, num: np.ndarray, fs: float = FS, win_s: float = WIN_S) -> dict:
    """wave: (n, 2) ECG_II, PLETH at fs; num: (m, 4) at 1 Hz. Returns window arrays."""
    ecg, ppg = wave[:, 0], wave[:, 1]
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
            e = ecg[a:b]
            pf = pat_features(np.where(np.isnan(e), 0.0, e), seg, fs) if np.isnan(e).mean() < 0.02 \
                else np.full(N_PAT, np.nan)
        if np.isnan(f).all():
            continue
        T.append(b / fs); F.append(f); P.append(pf); L.append(lab)
    ns, nd = num[:, 2], num[:, 3]
    k = np.where(np.isfinite(ns) & np.isfinite(nd))[0]
    if k.size:
        new_val = np.r_[True, (np.diff(ns[k]) != 0) | (np.diff(nd[k]) != 0)]
        k = k[new_val | np.r_[True, np.diff(k) > 20]]
    nibp = np.column_stack([k * 1.0, ns[k], nd[k]]) if k.size else np.zeros((0, 3))
    nibp = nibp[(nibp[:, 1] > 40) & (nibp[:, 1] < 260)] if len(nibp) else nibp
    return {"t": np.array(T), "F": np.array(F).reshape(-1, 26), "P": np.array(P).reshape(-1, N_PAT),
            "L": np.array(L).reshape(-1, 2), "nibp": nibp}


def fetch(caseid: int, subjectid: int, out_dir: Path) -> str:
    import vitaldb
    out = out_dir / f"{caseid}.npz"
    if out.exists():
        return "cached"
    wave = vitaldb.load_case(caseid, WAVES, 1 / FS)
    if wave is None or len(wave) == 0 or np.isnan(np.asarray(wave, float)[:, 0]).all():
        np.savez_compressed(out, empty=True, version=FEATURE_VERSION, pat_version=PAT_VERSION)
        return "no ECG_II, skipped"
    num = vitaldb.load_case(caseid, NUMS, 1.0)
    r = process(np.asarray(wave, dtype=np.float64), np.asarray(num, dtype=np.float64))
    np.savez_compressed(out, version=FEATURE_VERSION, pat_version=PAT_VERSION, caseid=caseid, subjectid=subjectid,
                        t=r["t"], F=r["F"], P=r["P"], L=r["L"], nibp=r["nibp"])
    good = np.isfinite(r["P"][:, 1]).mean() * 100 if len(r["P"]) else 0
    return f"{len(r['t'])} windows, PAT found in {good:.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-cases", type=int, default=150)
    ap.add_argument("--threads", type=int, default=2, help="parallel downloads (each holds ~100 MB in memory)")
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"), help="existing PPG cache (for case ids)")
    ap.add_argument("--out", default=str(ROOT / "vitaldb_pat_cache"))
    ap.add_argument("--seed", type=int, default=42, help="patient split seed (must match vitaldb_train.py)")
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
    print(f"{len(chosen)} cases chosen (70/15/15 of the existing split), {len(chosen) - len(todo)} already done, "
          f"{len(todo)} to download", flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(fetch, cid, sid, out): cid for cid, sid in todo}
        for fut in as_completed(futs):
            done += 1
            try:
                msg = fut.result()
            except Exception as exc:     # network hiccup: rerun later to retry
                msg = f"failed ({exc.__class__.__name__}: {exc})"
            eta = (time.time() - t0) / done * (len(todo) - done) / 60
            print(f"[{done}/{len(todo)}] case {futs[fut]}: {msg}   ~{eta:.0f} min left", flush=True)
    print(f"done. PAT cache in {out}")


if __name__ == "__main__":
    main()
