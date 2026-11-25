"""Step 2. Does the model run, and does its output move when the input changes?

Feeds synthetic MAX30102-style IR through the same path siren_app uses
(25 Hz raw counts -> invert -> 125 Hz -> rescale -> features -> forest).

    python scripts/smoke_test.py
"""
import _path  # noqa: F401

import numpy as np

from bp_model import FEATURE_NAMES, BpModel, extract_features, prepare_window
from bp_model.synthetic import max30102_ir

m = BpModel()

cases = [
    ("baseline HR 75", dict(hr_bpm=75)),
    ("tachycardia HR 120", dict(hr_bpm=120)),
    ("bradycardia HR 50", dict(hr_bpm=50)),
    ("stiff artery (fast rise, no notch)", dict(rise=0.08, notch=0.0)),
    ("compliant artery (slow rise, big notch)", dict(rise=0.22, notch=0.45)),
    ("weak pulse (AC 0.2%)", dict(ac_frac=0.002)),
    ("strong pulse (AC 3%)", dict(ac_frac=0.03)),
    ("fs 10 Hz", dict(fs=10.0)),
]

print(f"{'case':<42}{'SBP':>7}{'DBP':>7}  status")
preds, feats = [], []
for name, kw in cases:
    fs = kw.pop("fs", 25.0)
    t, ir = max30102_ir(32, fs=fs, **kw)
    sbp, dbp, status = m.predict_from_sensor(t, ir)
    print(f"{name:<42}{sbp:>7.1f}{dbp:>7.1f}  {status}")
    preds.append((sbp, dbp))
    w, _, _ = prepare_window(t, ir, m.reference)
    feats.append(extract_features(w))

preds = np.array(preds)
feats = np.array(feats)
print(f"\nPrediction range across very different inputs  SBP {np.ptp(preds[:,0]):.1f} mmHg, DBP {np.ptp(preds[:,1]):.1f} mmHg")
print("For comparison, the NEWS2 SBP bands are 10 mmHg wide.")

rel = np.ptp(feats, axis=0) / (np.abs(feats.mean(axis=0)) + 1e-9)
fixed = [FEATURE_NAMES[i] for i in np.where(rel < 1e-6)[0]]
print(f"\nFeatures that did not change at all across these {len(cases)} inputs ({len(fixed)} of 36)")
print("  " + ", ".join(fixed))
print("Rescaling in prepare_window fixes the first six; the median counts are constant by\n"
      "construction (always about half of 3750). The forest cannot use any of them live.")

# Same input twice must give the same answer
t, ir = max30102_ir(32)
assert m.predict_from_sensor(t, ir) == m.predict_from_sensor(t, ir), "not deterministic"
# A gain change (DC x2) must not change the answer, since the window is rescaled
t, ir2 = max30102_ir(32, dc=240000)
d = abs(m.predict_from_sensor(t, ir)[0] - m.predict_from_sensor(t, ir2)[0])
print(f"\nLED gain doubled -> SBP change {d:.2f} mmHg (expected about 0)")
print("\nSmoke test finished without errors.")
