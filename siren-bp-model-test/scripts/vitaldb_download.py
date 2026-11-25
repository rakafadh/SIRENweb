"""Download VitalDB cases and turn them into SIREN features, time-ordered.

    pip install vitaldb
    python scripts/vitaldb_download.py --n-cases 150

VitalDB (Lee et al., Sci Data 2022; open access, CC BY 4.0) has 6,388
surgical cases with a finger pulse-oximeter waveform (SNUADC/PLETH, 500 Hz)
and, in many cases, an arterial line (SNUADC/ART with the monitor's
Solar8000/ART_SBP and ART_DBP numbers) recorded on one clock for hours.
Unlike MIMIC-BP, consecutive windows are minutes apart, so BP changes over
2 to 15 minutes can be learned and tested.

For every selected case this script
  1. downloads PLETH, ART_SBP/ART_DBP (reference BP) and NIBP_SBP/NIBP_DBP
     (real cuff readings taken during surgery) at 100 Hz,
  2. cuts PLETH into back-to-back 30 s windows, brings each to 25 Hz (the
     MAX30102 rate) and extracts the same 26 features as features_v2.py,
  3. labels each window with the median arterial SBP/DBP inside it, after
     rejecting artefacts (line flush, zeroing, damped trace),
  4. saves one small file per case: vitaldb_cache/<caseid>.npz.

Raw waveforms are not kept, so disk use stays small. Interrupted runs resume:
cases already in the cache are skipped. Network use is roughly 10 to 40 MB per
case; start with --n-cases 50 to check speed.
"""
import _path  # noqa: F401
import argparse
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from bp_model.features_v2 import FEATURE_VERSION, extract
from scipy.signal import decimate

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.vitaldb.net"
FS_LOAD = 100.0         # Hz requested from VitalDB
WIN_S = 30.0
TRACKS = ["SNUADC/PLETH", "Solar8000/ART_SBP", "Solar8000/ART_DBP",
          "Solar8000/NIBP_SBP", "Solar8000/NIBP_DBP"]


# ------------------------------------------------------------- processing
def window_label(sbp: np.ndarray, dbp: np.ndarray):
    """Median arterial BP in one window, or None when the trace looks wrong."""
    s, d = sbp[np.isfinite(sbp)], dbp[np.isfinite(dbp)]
    if s.size < 5 or d.size < 5:
        return None
    ms, md = float(np.median(s)), float(np.median(d))
    if not (50 <= ms <= 250 and 25 <= md <= 150 and ms - md >= 15):
        return None                      # zeroing, flush, damped line
    if np.ptp(s) > 40 or np.ptp(d) > 30:
        return None                      # flush or movement inside the window
    return ms, md


def process_case(vals: np.ndarray, fs: float = FS_LOAD, win_s: float = WIN_S) -> dict:
    """vals: (n, 5) array in TRACKS order at fs Hz. Returns window arrays."""
    pleth = vals[:, 0]
    n_win = int(len(pleth) // (fs * win_s))
    step = int(fs / 25.0)
    T, F, L = [], [], []
    for w in range(n_win):
        a, b = int(w * fs * win_s), int((w + 1) * fs * win_s)
        lab = window_label(vals[a:b, 1], vals[a:b, 2])
        if lab is None:
            continue
        seg = pleth[a:b]
        if np.isnan(seg).mean() > 0.02:
            continue
        seg = np.where(np.isnan(seg), np.nanmedian(seg), seg)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ppg25 = decimate(seg, step, ftype="fir", zero_phase=True) if step > 1 else seg
            f = extract(ppg25)
        if np.isnan(f).all():
            continue
        T.append(b / fs)                 # window end, seconds from case start
        F.append(f)
        L.append(lab)
    ns, nd = vals[:, 3], vals[:, 4]
    k = np.where(np.isfinite(ns) & np.isfinite(nd))[0]
    if k.size:                           # one entry per cuff cycle: new value or > 20 s gap
        new_val = np.r_[True, (np.diff(ns[k]) != 0) | (np.diff(nd[k]) != 0)]
        k = k[new_val | np.r_[True, np.diff(k) > fs * 20]]
    nibp = np.column_stack([k / fs, ns[k], nd[k]]) if k.size else np.zeros((0, 3))
    nibp = nibp[(nibp[:, 1] > 40) & (nibp[:, 1] < 260)] if len(nibp) else nibp
    return {"t": np.array(T), "F": np.array(F).reshape(-1, 26), "L": np.array(L).reshape(-1, 2), "nibp": nibp}


# --------------------------------------------------------------- download
def choose_cases(n: int, seed: int, min_age: int) -> list[tuple[int, int, float]]:
    import pandas as pd
    cases = pd.read_csv(f"{API}/cases")
    trks = pd.read_csv(f"{API}/trks")
    have = None
    for name in ("SNUADC/PLETH", "Solar8000/ART_SBP"):
        ids = set(trks.loc[trks["tname"] == name, "caseid"])
        have = ids if have is None else have & ids
    c = cases[cases["caseid"].isin(have)]
    if "age" in c:
        c = c[pd.to_numeric(c["age"], errors="coerce") >= min_age]
    if "ane_type" in c:
        c = c[c["ane_type"].astype(str).str.contains("General", case=False, na=False)]
    print(f"{len(c)} cases have PLETH + arterial line (adult, general anaesthesia)")
    c = c.sample(n=min(n, len(c)), random_state=seed)
    subj = c["subjectid"] if "subjectid" in c else c["caseid"]
    age = pd.to_numeric(c["age"], errors="coerce") if "age" in c else np.nan
    return list(zip(c["caseid"].astype(int), subj.astype(int), np.asarray(age, dtype=float)))


def fetch_one(caseid: int, subjectid: int, age: float, cache: Path) -> str:
    import vitaldb
    out = cache / f"{caseid}.npz"
    if out.exists():
        return "cached"
    vals = vitaldb.load_case(caseid, TRACKS, 1 / FS_LOAD)
    if vals is None or len(vals) == 0:
        np.savez_compressed(out, empty=True, version=FEATURE_VERSION)
        return "no data"
    r = process_case(np.asarray(vals, dtype=np.float64))
    np.savez_compressed(out, version=FEATURE_VERSION, caseid=caseid, subjectid=subjectid, age=age,
                        t=r["t"], F=r["F"], L=r["L"], nibp=r["nibp"])
    return f"{len(r['t'])} windows, {len(r['nibp'])} cuff readings"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-cases", type=int, default=150)
    ap.add_argument("--threads", type=int, default=4, help="parallel downloads")
    ap.add_argument("--min-age", type=int, default=18)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache", default=str(ROOT / "vitaldb_cache"))
    args = ap.parse_args()

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    chosen = choose_cases(args.n_cases, args.seed, args.min_age)
    todo = [c for c in chosen if not (cache / f"{c[0]}.npz").exists()]
    print(f"{len(chosen)} cases selected, {len(chosen) - len(todo)} already cached, {len(todo)} to download")
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(fetch_one, cid, sid, age, cache): cid for cid, sid, age in todo}
        for fut in as_completed(futs):
            done += 1
            cid = futs[fut]
            try:
                msg = fut.result()
            except Exception as exc:     # network hiccup: rerun later to retry
                msg = f"failed ({exc.__class__.__name__}: {exc})"
            eta = (time.time() - t0) / done * (len(todo) - done) / 60
            print(f"[{done}/{len(todo)}] case {cid}: {msg}   ~{eta:.0f} min left", flush=True)
    print(f"done. cache in {cache}")


if __name__ == "__main__":
    main()
