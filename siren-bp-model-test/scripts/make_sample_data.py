"""Regenerates sample_data/. Synthetic only, for checking the scripts run.

    python scripts/make_sample_data.py
"""
import _path  # noqa: F401
from pathlib import Path

import numpy as np

from bp_model.synthetic import max30102_ir, upright_ppg

root = Path(__file__).resolve().parent.parent / "sample_data"

# 1. A serial log in the firmware's format, with console lines mixed in
t, ir = max30102_ir(70, fs=25.0, hr_bpm=82, seed=1)
lines = ["MAX30102 ready.", "Rest a fingertip lightly on the sensor and breathe normally."]
for b in range(0, t.size - 24, 25):
    blk = ir[b:b + 25].astype(int)
    lines.append("1234,98,82,16,36.6,0.012,ok")  # stand-in for the CSV row
    lines.append(f"W,{int(t[b + 24]) + 4000},25.00," + ",".join(map(str, blk)))
(root / "sample_serial_log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

# 2. A tiny fake MIMIC-BP folder (3 test + 3 train subjects, 3 windows each)
fm = root / "fake_mimic"
(fm / "ppg").mkdir(parents=True, exist_ok=True)
(fm / "labels").mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(0)
for split, ids in (("test", ["f01", "f02", "f03"]), ("train", ["f11", "f12", "f13"])):
    (fm / f"{split}_subjects.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
    for k, sid in enumerate(ids):
        wins, labs = [], []
        for w in range(3):
            _, s = upright_ppg(30, 125.0, hr_bpm=60 + 15 * k, rise=0.1 + 0.04 * k, seed=w)
            s = (s - s.mean()) / s.std() * 0.42 + 1.67
            wins.append(s[:3750].astype(np.float32))
            labs.append([110 + 15 * k + rng.normal(0, 4), 65 + 8 * k + rng.normal(0, 3)])
        np.save(fm / "ppg" / f"{sid}_ppg.npy", np.array(wins))
        np.save(fm / "labels" / f"{sid}_labels.npy", np.array(labs, dtype=np.float32))
print("sample_data regenerated")
