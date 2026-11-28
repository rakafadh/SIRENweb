"""Readable live view of the SIREN PPG node, with RR shown clearly.

    python tools\\siren_monitor.py --port COM3
    python tools\\siren_monitor.py --replay results\\rr\\20261128_180000_raw.txt

One row per second: time, HR, SpO2, PI, RR and, when there is no RR, why not.
RR is green when there is a value and "--" with the reason when there is not.
With firmware v4 the reason comes from the board (R lines); with older
firmware it is worked out from the three per-signal estimates.

Breathing-rate test (metronome or counted breaths): while it runs, type the
rate you are breathing at and press Enter, for example 12. Type 0 when you
stop. The rows then show the target, and Ctrl+C prints a summary per block
(share of seconds with an RR, median RR, error against the target).

Everything the board sends is saved unchanged to results/rr/<time>_raw.txt
(your typed targets as M,<ms>,<rate> lines), so the file can be replayed here
or with tools/rr_bench/replay_log.cpp. The readable rows go to <time>_rr.csv.

The firmware needs CSV_OUTPUT 1 (the default). Needs pyserial
(pip install pyserial), which the siren-bp-model-test venv already has.
"""
import argparse
import math
import os
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N_COLS = 23
FINGER_IR_MIN = 50000      # main_ppg.cpp
IR_SATURATED = 220000
DC_DRIFT_MAX = 0.10
PI_MIN = 0.15
RESP_MIN_BEATS = 8         # respiration.h

GREEN, GREY, YELLOW, BOLD, RESET = "\033[92m", "\033[90m", "\033[93m", "\033[1m", "\033[0m"


def num(s):
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_row(parts):
    keys = ["ms", "fs", "led", "ir", "drift", "pi", "hr", "hr_src", "sdnn", "intervals", "spo2", "rr",
            "riiv", "riav", "rifv", "spread", "beats", "tobj", "tamb", "tcore", "acc", "acc_peak", "gyro"]
    r = dict(zip(keys, parts))
    out = {k: num(v) for k, v in r.items() if k != "hr_src"}
    out["hr_src"] = r["hr_src"]
    for k in ("hr", "spo2", "rr", "sdnn"):
        if out[k] is not None and out[k] < 0:
            out[k] = None
    return out


def signal_state(r):
    if r["ir"] is None or r["ir"] < FINGER_IR_MIN:
        return "no finger"
    if r["ir"] > IR_SATURATED:
        return "saturated"
    if r["drift"] is not None and r["drift"] > DC_DRIFT_MAX:
        return "moving"
    if r["pi"] is not None and r["pi"] < PI_MIN:
        return "weak pulse"
    return "ok"


NOTE_TEXT = {   # firmware v4 R-line notes
    "ok": "RR ok", "prelim": "RR ok (first 32 s, preliminary)", "slow": "RR ok (slow breathing)",
    "tracking": "RR tracking (1 signal, follows the last agreed rate)",
    "fast": "RR ok (fast breathing, RIIV only)",
    "filling": "collecting 16 s clean signal", "artefact": "paused (movement), collecting again",
    "few beats": "waiting for beats", "few sources": "only 1 signal clear",
    "disagree": "signals disagree", "fast?": "beats missed, possible fast breathing: COUNT MANUALLY",
    "no finger": "no finger", "starting": "starting",
}


def rr_reason(r, state):
    """Why there is no RR, for firmware without R lines: read off the three
    per-signal estimates."""
    if state != "ok":
        return {"no finger": "no finger", "saturated": "paused (signal saturated)",
                "moving": "paused (finger moving)", "weak pulse": "paused (weak pulse)"}[state]
    got = [v for v in (r["riiv"], r["riav"], r["rifv"]) if v is not None]
    if not got:
        if (r["beats"] or 0) < RESP_MIN_BEATS:
            return "waiting for beats"
        return "collecting 16 s clean signal"
    if len(got) == 1:
        return "only 1 signal clear"
    return "signals disagree"


def fmt(v, nd=0, width=5):
    return f"{'--':>{width}}" if v is None else f"{v:>{width}.{nd}f}"


class Monitor:
    def __init__(self, color, csv_fh):
        self.c = color
        self.csv = csv_fh
        self.target = None
        self.rows = 0
        self.blocks = []          # [target, rr values or None per second]
        self.note = None          # latest R-line note from the board (firmware v4)
        self.ceiling = None
        self.lock = threading.Lock()
        csv_fh.write("t_s,target,hr,spo2,pi,rr,riiv,riav,rifv,spread,rr_status,signal,rr_ceiling\n")

    def r_line(self, parts):
        # R,<ms>,<note>,<signals used>,<ceiling>
        if len(parts) >= 5:
            self.note = parts[2]
            self.ceiling = num(parts[4])

    def col(self, code, s):
        return f"{code}{s}{RESET}" if self.c else s

    def header(self):
        h = (f"{'time':>6}  {'target':>6}  {'HR':>4}  {'SpO2':>4}  {'PI%':>5}  |  {'RR':>5}  "
             f"{'RIIV':>5} {'RIAV':>5} {'RIFV':>5}  status")
        print(self.col(BOLD, h))
        print("-" * len(h))

    def set_target(self, rate, ms):
        with self.lock:
            self.target = rate if rate else None
            if self.target:
                self.blocks.append([self.target, []])
        msg = f"target set to {rate:g} breaths/min" if rate else "target cleared"
        print(self.col(YELLOW, f"  >> {msg}"), flush=True)

    def row(self, r):
        state = signal_state(r)
        t = r["ms"] / 1000.0
        if self.rows % 20 == 0:
            self.header()
        self.rows += 1
        note, self.note = self.note, None
        if r["rr"] is not None:
            rr_s = self.col(GREEN + BOLD, fmt(r["rr"], 1))
            if note in NOTE_TEXT and note not in ("ok",):
                text = NOTE_TEXT[note]
            else:
                n = sum(v is not None and abs(v - r["rr"]) <= 4 for v in (r["riiv"], r["riav"], r["rifv"]))
                text = f"RR ok ({max(n, 2)} of 3 signals agree)"
            status = self.col(GREEN if note != "tracking" else YELLOW, text)
            reason = note or "ok"
        else:
            rr_s = self.col(GREY, fmt(None))
            if note == "fast?":
                text = self.col(YELLOW + BOLD, NOTE_TEXT[note])
            else:
                text = self.col(GREY, NOTE_TEXT.get(note, rr_reason(r, state)) if note else rr_reason(r, state))
            status = text
            reason = note or rr_reason(r, state)
        if state != "ok" and r["rr"] is not None:
            status += self.col(YELLOW, f"  [{state}]")
        with self.lock:
            tgt = self.target
            if tgt and self.blocks:
                self.blocks[-1][1].append(r["rr"])
        line = (f"{t:>5.1f}s  {fmt(tgt, 0, 6)}  {fmt(r['hr'], 0, 4)}  {fmt(r['spo2'], 0, 4)}  "
                f"{fmt(r['pi'], 2, 5)}  |  {rr_s}  {fmt(r['riiv'], 1)} {fmt(r['riav'], 1)} "
                f"{fmt(r['rifv'], 1)}  {status}")
        print(line, flush=True)
        vals = [t, tgt, r["hr"], r["spo2"], r["pi"], r["rr"], r["riiv"], r["riav"], r["rifv"], r["spread"]]
        self.csv.write(",".join("" if v is None else f"{v:g}" for v in vals)
                       + f",{reason},{state},{'' if self.ceiling is None else f'{self.ceiling:g}'}\n")
        self.csv.flush()

    def summary(self):
        if not self.blocks:
            return
        print()
        print(self.col(BOLD, "Breathing test summary"))
        print(f"{'target':>6}  {'seconds':>7}  {'RR shown':>8}  {'median RR':>9}  {'mean error':>10}  {'within 2/min':>12}")
        for tgt, vals in self.blocks:
            got = [v for v in vals if v is not None]
            if not vals:
                continue
            avail = 100 * len(got) / len(vals)
            if got:
                med = statistics.median(got)
                err = statistics.mean(abs(v - tgt) for v in got)
                w2 = 100 * sum(abs(v - tgt) <= 2 for v in got) / len(got)
                print(f"{tgt:>6g}  {len(vals):>7}  {avail:>7.0f}%  {med:>9.1f}  {err:>10.1f}  {w2:>11.0f}%")
            else:
                print(f"{tgt:>6g}  {len(vals):>7}  {avail:>7.0f}%  {'--':>9}  {'--':>10}  {'--':>12}")
        print("Mean error and within 2/min are over the seconds that showed an RR.")


def keyboard(mon, raw_fh, last_ms):
    for text in sys.stdin:
        text = text.strip()
        if not text:
            continue
        try:
            rate = float(text)
        except ValueError:
            print("  type a number (breaths/min), or 0 to clear", flush=True)
            continue
        mon.set_target(rate, last_ms[0])
        raw_fh.write(f"M,{last_ms[0]:.0f},{rate:g}\n")
        raw_fh.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--replay", help="raw log to show instead of the serial port")
    ap.add_argument("--speed", type=float, default=0, help="--replay: 1 = real time, 0 = as fast as possible")
    ap.add_argument("--out", default=str(ROOT / "results" / "rr"))
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if not args.port and not args.replay:
        ap.error("give --port COM3 (or --replay <file>)")

    color = not args.no_color and sys.stdout.isatty()
    if color and os.name == "nt":
        os.system("")                      # switches on colour codes in the Windows console
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    raw_path = out / f"{stamp}_raw.txt" if not args.replay else None
    csv_path = out / (f"{stamp}_rr.csv" if not args.replay else Path(args.replay).stem.replace("_raw", "") + "_rr.csv")

    raw_fh = open(raw_path, "w", encoding="utf-8", buffering=1) if raw_path else open(os.devnull, "w")
    csv_fh = open(csv_path, "w", encoding="utf-8")
    mon = Monitor(color, csv_fh)
    last_ms = [0.0]

    if args.replay:
        src = open(args.replay, encoding="utf-8", errors="replace")
        print(f"replaying {args.replay}")
    else:
        import serial
        ser = serial.Serial(args.port, args.baud, timeout=1)
        src = iter(lambda: ser.readline(), None)
        print(f"listening on {args.port}. Saving to {raw_path}")
        print("Type your breathing rate + Enter to mark a test block (e.g. 12), 0 to clear, Ctrl+C to stop.")
        threading.Thread(target=keyboard, args=(mon, raw_fh, last_ms), daemon=True).start()

    prev = None
    try:
        for line in src:
            if isinstance(line, bytes):
                if not line:
                    continue
                line = line.decode("utf-8", errors="replace")
            line = line.strip()
            if " > " in line[:20]:           # pio monitor timestamps ("17:54:40.363 > ...")
                line = line.split(" > ", 1)[1]
            if not line:
                continue
            raw_fh.write(line + "\n")
            if line.startswith(("W,", "G,")):
                continue
            if line.startswith("R,"):          # RR status for the next CSV row (firmware v4)
                mon.r_line(line.split(","))
                continue
            if line.startswith("M,"):          # a target typed during the recording
                p = line.split(",")
                if len(p) >= 3 and num(p[2]) is not None:
                    mon.set_target(num(p[2]), num(p[1]) or 0)
                continue
            parts = line.split(",")
            if len(parts) == N_COLS and parts[0].isdigit():
                r = parse_row(parts)
                last_ms[0] = r["ms"]
                if args.replay and args.speed and prev is not None:
                    time.sleep(max(0.0, (r["ms"] - prev) / 1000 / args.speed))
                prev = r["ms"]
                mon.row(r)
            elif not line[0].isdigit():
                print(mon.col(GREY, f"  [board] {line}"), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        mon.summary()
        raw_fh.close()
        csv_fh.close()
        if raw_path:
            print(f"\nraw log  {raw_path}")
        print(f"table    {csv_path}")


if __name__ == "__main__":
    main()
