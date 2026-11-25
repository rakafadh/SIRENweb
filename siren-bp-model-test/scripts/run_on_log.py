"""Step 4. Run the model on a real SIREN serial log, the W lines from main_ppg.

    python scripts/run_on_log.py sample_data/sample_serial_log.txt
    python scripts/run_on_log.py my_log.txt --cuff 118/76

Slides a 30 s window over the recording every --step seconds, like siren_app
does live, and writes one row per window. With --cuff, each estimate is
compared with that cuff reading (take it during the recording).
"""
import _path  # noqa: F401
import argparse
import csv
from pathlib import Path

import numpy as np

from bp_model import BpModel, news2_sbp_score, parse_waveform_log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--cuff", help="reference reading, e.g. 120/80")
    ap.add_argument("--step", type=float, default=5.0, help="seconds between estimates")
    ap.add_argument("--out", default="results/log_predictions.csv")
    args = ap.parse_args()

    t, ir = parse_waveform_log(args.log)
    if t.size == 0:
        raise SystemExit("No W lines found. Flash firmware with WAVEFORM_OUTPUT 1 and save the serial monitor output.")
    dur = (t[-1] - t[0]) / 1000
    fs = (t.size - 1) / dur if dur > 0 else 0
    print(f"{t.size} samples, {dur:.1f} s, about {fs:.1f} Hz")

    cuff = None
    if args.cuff:
        s, d = args.cuff.split("/")
        cuff = (float(s), float(d))

    m = BpModel()
    rows = []
    end = t[0] + 30000
    while end <= t[-1] + 1:
        sel = (t > end - 30000 - 1000) & (t <= end)   # a little extra so coverage reaches 30 s
        sbp, dbp, status = m.predict_from_sensor(t[sel], ir[sel])
        rows.append([round((end - t[0]) / 1000, 1), status,
                     None if sbp is None else round(sbp, 1), None if dbp is None else round(dbp, 1)])
        end += args.step * 1000

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["t_end_s", "status", "sbp", "dbp"])
        wr.writerows(rows)

    ok = np.array([[r[2], r[3]] for r in rows if r[1] == "OK"], dtype=float)
    print(f"{len(rows)} windows, {len(ok)} OK, others: {sorted({r[1] for r in rows if r[1] != 'OK'})}")
    if len(ok):
        print(f"SBP mean {ok[:,0].mean():.1f}  range {ok[:,0].min():.1f}-{ok[:,0].max():.1f}  "
              f"NEWS2 SBP score {news2_sbp_score(ok[:,0].mean())}")
        print(f"DBP mean {ok[:,1].mean():.1f}  range {ok[:,1].min():.1f}-{ok[:,1].max():.1f}")
        if cuff:
            e = ok - np.array(cuff)
            print(f"vs cuff {args.cuff}  SBP error {e[:,0].mean():+.1f} (SD {e[:,0].std():.1f})  "
                  f"DBP error {e[:,1].mean():+.1f} (SD {e[:,1].std():.1f})")
            print(f"NEWS2 SBP score  cuff {news2_sbp_score(cuff[0])}  model {news2_sbp_score(ok[:,0].mean())}")
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
