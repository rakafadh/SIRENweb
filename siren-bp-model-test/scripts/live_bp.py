"""Live BP from the SIREN PPG board (MAX30102), with cuff readings typed in.

    pip install pyserial
    python scripts/live_bp.py --port COM13

Firmware: siren-sensors, env ppg, with WAVEFORM_OUTPUT 1 in src/ppg/main_ppg.cpp
(prints the raw IR as W lines). Close the PlatformIO monitor and the dashboard
first: only one program can hold the serial port.

Settings are the final ones (model/bp_calibrated_vitaldb.joblib +
alert_settings_vitaldb.json, schedule "smart_kalman"): Kalman fusion, cuff every
10 min, extra cuff on any 5 mmHg change or NEWS2 band change, NEWS2 SBP score from
estimate - 0.3 x sigma, last estimate held while a requested cuff is pending.

Type a command and press Enter:
  s          cuff started NOW: keeps the 30 s PPG window from just before inflation
  118/76     cuff reading: calibrates with the window saved by "s" (if you forgot
             "s", with the window that ended --entry-lag seconds ago) and prints
             the estimate just before it, so you see the error
  c 118/76   check only: compares with the estimate, no calibration
  q          quit and print the error summary

Writes results/live/<time>_raw.txt (every serial line; replay with
    python scripts/run_calibrated_on_log.py results/live/<time>_raw.txt --model vitaldb --cuff 118/76)
and results/live/<time>_bp.csv (every estimate and cuff).
--replay FILE feeds a saved raw log instead of the serial port (testing).
"""
import _path  # noqa: F401
import argparse
import csv
import math
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from bp_model.calibrated import MODEL_DIR, CalibratedBpModel

ROOT = Path(__file__).resolve().parent.parent
W_LINE = re.compile(r"W,(\d+),([\d.]+),([\d,]+)")
G_LINE = re.compile(r"G,(\d+),(\d+)")
CUFF = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")
KEEP_MS = 15 * 60 * 1000          # waveform kept in memory
WIN_MS = 31000                    # window handed to the model (it uses the newest 30 s)


class Stream:
    """Raw IR samples with device timestamps, filled by the reader thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.t, self.ir = [], []
        self.gain_ms = []             # LED changes: windows across them are skipped
        self.hr = math.nan
        self.accel = math.nan
        self.last_ms = None           # device time of the newest sample
        self.resets = 0
        self.ended = False
        self.n_lines = self.n_csv = 0     # for diagnosing "no signal"
        self.last_line = ""

    def diagnose(self):
        if self.n_lines == 0:
            return ("nothing received from the port. Wrong port (try another from `pio device list`), "
                    "or press the board's EN/RST button")
        if self.n_csv:
            return ("the board sends CSV rows but no W lines: upload the new ppg firmware "
                    "(WAVEFORM_OUTPUT 1) and run this again")
        return f"no W lines yet; last line from the board: {self.last_line[:80]!r}"

    def add_block(self, end_ms, fs, samples):
        n = len(samples)
        with self.lock:
            if self.last_ms is not None and end_ms < self.last_ms - 5000:   # board restarted
                self.t, self.ir, self.gain_ms = [], [], []
                self.resets += 1
            for i, v in enumerate(samples):
                self.t.append(end_ms - (n - 1 - i) * 1000.0 / fs)
                self.ir.append(v)
            self.last_ms = end_ms
            if self.t and self.t[-1] - self.t[0] > KEEP_MS:
                k = np.searchsorted(self.t, self.t[-1] - KEEP_MS)
                del self.t[:k], self.ir[:k]

    def window(self, end_ms):
        """(times, ir) ending at end_ms, or (None, reason)."""
        with self.lock:
            if not self.t or self.t[-1] - self.t[0] < WIN_MS - 1000:
                return None, "COLLECTING"
            if any(end_ms - WIN_MS < g <= end_ms for g in self.gain_ms):
                return None, "GAIN_CHANGE"
            t = np.asarray(self.t)
            sel = (t > end_ms - WIN_MS) & (t <= end_ms)
            if sel.sum() < 25 * 25:
                return None, "COLLECTING"
            return (t[sel], np.asarray(self.ir, dtype=np.float64)[sel]), "OK"


def reader(src, stream, raw_fh, speed):
    """Reads serial lines (or replays a file), logs them, fills the stream."""
    prev_ms, prev_wall = None, None
    for line in src:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        raw_fh.write(line + "\n")
        stream.n_lines += 1
        stream.last_line = line
        m = W_LINE.search(line)
        if m:
            end_ms, fs = float(m.group(1)), float(m.group(2))
            samples = [int(v) for v in m.group(3).strip(",").split(",") if v]
            if speed:                                         # replay: keep real-time pacing
                if prev_ms is not None and end_ms > prev_ms:
                    wait = (end_ms - prev_ms) / 1000 / speed - (time.monotonic() - prev_wall)
                    if wait > 0:
                        time.sleep(wait)
                prev_ms, prev_wall = end_ms, time.monotonic()
            if fs > 0 and samples:
                stream.add_block(end_ms, fs, samples)
            continue
        m = G_LINE.search(line)
        if m:
            with stream.lock:
                stream.gain_ms.append(float(m.group(1)))
            continue
        parts = line.split(",")
        if len(parts) == 23 and parts[0].isdigit():           # the firmware's CSV row
            stream.n_csv += 1
            try:
                stream.hr = float(parts[6])
                stream.accel = float(parts[20])
            except ValueError:
                pass
        elif not line[0].isdigit():
            print(f"  [board] {line}", flush=True)
    stream.ended = True


def fmt(x, nd=0):
    return "--" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


class Live:
    def __init__(self, args, stream, out_csv):
        self.args, self.stream = args, stream
        self.model = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib",
                                       MODEL_DIR / "alert_settings_vitaldb.json")
        self.mlock = threading.Lock()
        self.saved = None             # (device ms, window) kept by "s"
        self.last_est = None          # (device ms, estimate dict) newest OK estimate
        self.cuff_on = False
        self.t0 = None
        self.checks = []              # (cuff sbp, cuff dbp, est sbp, est dbp, calibrated?)
        self.resets_seen = 0
        self.waited = 0               # ticks without any waveform
        self.quiet = (None, -1e9)     # (status, device ms) of the last status-only line printed
        self.fh = open(out_csv, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.fh)
        self.w.writerow(["wall_time", "t_s", "kind", "status", "sbp", "dbp", "live_sbp", "sbp_sigma",
                         "news2_sbp_score", "cuff_now", "reasons", "cuff_sbp", "cuff_dbp",
                         "est_before_sbp", "est_before_dbp", "result"])

    def row(self, t_ms, kind, **kw):
        ts = None if t_ms is None or self.t0 is None else round((t_ms - self.t0) / 1000, 1)
        keys = ["status", "sbp", "dbp", "live_sbp", "sbp_sigma", "news2_sbp_score", "cuff_now", "reasons",
                "cuff_sbp", "cuff_dbp", "est_before_sbp", "est_before_dbp", "result"]
        self.w.writerow([datetime.now().isoformat(timespec="seconds"), ts, kind] + [kw.get(k, "") for k in keys])
        self.fh.flush()

    def describe(self):
        m, c = self.model, self.model.config
        return (f"model {m.name}; schedule {m.schedule}; cuff every {c.max_interval_stable_min:g} min "
                f"(+ any {m.trigger_mmhg:g} mmHg / NEWS2 band change, min gap {c.min_cuff_gap_min:g} min); "
                f"NEWS2 score from estimate - {m.score_z:g} sigma; while waiting: {c.pending_display}")

    # ------------------------------------------------------------ estimates
    def status_line(self, end, t_s, st, hint):
        """No estimate: print when the status changes, else every 30 s."""
        if st != self.quiet[0] or end - self.quiet[1] >= 30000:
            print(f"t={t_s:6.0f}s  [{st}] {hint}", flush=True)
            self.quiet = (st, end)
        self.row(end, "estimate", status=st)

    def tick(self):
        s = self.stream
        if s.resets != self.resets_seen:
            self.resets_seen = s.resets
            with self.mlock:
                self.model.reset()
            self.saved = self.last_est = None
            self.cuff_on = False
            print("\n!! Board restarted: calibration cleared. Take a new cuff (s, then the reading).", flush=True)
        end = s.last_ms
        if end is None:
            self.waited += 1
            if self.waited * self.args.step >= 10 and self.waited % max(1, round(30 / self.args.step)) == 2:
                print(f"   no waveform after {self.waited * self.args.step:.0f} s: {s.diagnose()}", flush=True)
            return
        if self.t0 is None:
            self.t0 = end
        win, why = s.window(end)
        t_s = (end - self.t0) / 1000
        if win is None:
            self.status_line(end, t_s, why, "")
            return
        with self.mlock:
            r = self.model.estimate(*win)
        st = r["status"]
        if st in ("NEEDS_CALIBRATION", "COLLECTING", "POOR_SIGNAL"):
            hint = {"NEEDS_CALIBRATION": "no cuff yet: type s as the cuff starts, then the reading",
                    "POOR_SIGNAL": "keep the finger still, light contact"}.get(st, "")
            self.status_line(end, t_s, st, hint)
            return
        self.last_est = (end, r)
        self.quiet = (None, -1e9)
        sig = r.get("sbp_sigma")
        line = (f"t={t_s:6.0f}s  SBP {fmt(r['sbp'])} / DBP {fmt(r['dbp'])}"
                f"{'' if sig is None else f'  (±{1.96 * sig:.0f})'}  NEWS2 SBP {r['news2_sbp_score']}"
                f"  [{st}]  {r['minutes_since_cuff']:.1f} min since cuff, next due {r['next_cuff_due_min']:.1f}")
        if r.get("held"):
            line += f"  held, live {fmt(r['live_sbp'])}"
        if not math.isnan(s.hr):
            line += f"  HR {s.hr:.0f}"
        print(line, flush=True)
        if r["cuff_now"] and not self.cuff_on:
            print(f"   >>> CUFF NOW ({', '.join(r['recalibrate_reasons'])}): type s as it starts, then the reading",
                  flush=True)
        self.cuff_on = r["cuff_now"]
        self.row(end, "estimate", status=st, sbp=r["sbp"], dbp=r["dbp"], live_sbp=r["live_sbp"], sbp_sigma=sig or "",
                 news2_sbp_score=r["news2_sbp_score"], cuff_now=int(r["cuff_now"]),
                 reasons=" ".join(r["recalibrate_reasons"]))

    # ------------------------------------------------------------- commands
    def command(self, text):
        text = text.strip().lower()
        s = self.stream
        if not text:
            return True
        if text in ("q", "quit", "exit"):
            return False
        if text == "s":
            if s.last_ms is None:
                print(f"   no signal yet: {s.diagnose()}", flush=True)
                return True
            win, why = s.window(s.last_ms)
            if win is None:
                print(f"   cannot keep a window now ({why}); the reading will use the one before it", flush=True)
                self.saved = None
            else:
                self.saved = (s.last_ms, win)
                print("   window saved. Keep the finger still; type the reading when the cuff shows it.", flush=True)
            self.row(s.last_ms, "cuff_start", result=why)
            return True
        m = CUFF.search(text)
        if not m:
            print("   commands: s | 118/76 | c 118/76 | q", flush=True)
            return True
        sbp, dbp = float(m.group(1)), float(m.group(2))
        if not (50 <= sbp <= 260 and 30 <= dbp <= 160 and dbp < sbp):
            print("   that reading looks wrong (SBP/DBP), not used", flush=True)
            return True
        est = None
        if self.last_est is not None and s.last_ms - self.last_est[0] < 60000:
            est = self.last_est[1]
        before = "" if est is None else (f"estimate before {est['live_sbp']:.0f}/{est['live_dbp']:.0f}, error "
                                         f"{est['live_sbp'] - sbp:+.0f}/{est['live_dbp'] - dbp:+.0f}")
        if text.startswith("c"):
            print(f"   check {sbp:.0f}/{dbp:.0f}: {before or 'no recent estimate'}", flush=True)
            if est is not None:
                self.checks.append((sbp, dbp, est["live_sbp"], est["live_dbp"], False))
            self.row(s.last_ms, "check", cuff_sbp=sbp, cuff_dbp=dbp,
                     est_before_sbp=est["live_sbp"] if est else "", est_before_dbp=est["live_dbp"] if est else "")
            return True
        if self.saved is not None and s.last_ms - self.saved[0] < 10 * 60000:
            t_cal, win = self.saved
            src = f"window saved at s, {(s.last_ms - t_cal) / 1000:.0f} s ago"
        else:
            t_cal = s.last_ms - self.args.entry_lag * 1000
            win, why = s.window(t_cal)
            src = f"no s typed: window ending {self.args.entry_lag:g} s ago"
            if win is None:
                print(f"   cannot calibrate ({why}). Type s when the next cuff starts.", flush=True)
                self.row(s.last_ms, "cuff", cuff_sbp=sbp, cuff_dbp=dbp, result=why)
                return True
        self.saved = None
        motion = None if math.isnan(s.accel) else s.accel
        with self.mlock:
            ok, why = self.model.calibrate(win[0], win[1], sbp, dbp, motion_g=motion)
        print(f"   cuff {sbp:.0f}/{dbp:.0f} ({src}) -> {why}" + (f"; {before}" if before else ""), flush=True)
        if not ok:
            print("   not used: keep the finger still and take another cuff in a minute", flush=True)
        if est is not None:
            self.checks.append((sbp, dbp, est["live_sbp"], est["live_dbp"], ok))
        self.cuff_on = False
        self.row(t_cal, "cuff", cuff_sbp=sbp, cuff_dbp=dbp, est_before_sbp=est["live_sbp"] if est else "",
                 est_before_dbp=est["live_dbp"] if est else "", result=why)
        return True

    def summary(self):
        if not self.checks:
            print("No cuff readings compared with an estimate.")
            return
        c = np.array([x[:4] for x in self.checks], dtype=float)
        e = c[:, 2:] - c[:, :2]
        print(f"\n{len(c)} cuff readings compared with the estimate just before them (first cuff excluded):")
        for j, name in enumerate(("SBP", "DBP")):
            a = np.abs(e[:, j])
            print(f"  {name}: mean error {e[:, j].mean():+.1f}, SD {e[:, j].std(ddof=1) if len(c) > 1 else 0:.1f}, "
                  f"MAE {a.mean():.1f}; within 5 / 10 / 15 mmHg {np.mean(a <= 5) * 100:.0f} / "
                  f"{np.mean(a <= 10) * 100:.0f} / {np.mean(a <= 15) * 100:.0f}%")
        print("  A handful of readings on one person is a smoke test, not a validation.")


def main():
    ap = argparse.ArgumentParser(description="SIREN live BP")
    ap.add_argument("--port", help="serial port, e.g. COM13")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--replay", help="raw log to replay instead of the serial port")
    ap.add_argument("--speed", type=float, default=1.0, help="--replay: playback speed")
    ap.add_argument("--step", type=float, default=5.0, help="seconds between estimates")
    ap.add_argument("--entry-lag", type=float, default=45.0,
                    help="if no s was typed: the cuff window ends this many seconds before the reading is typed")
    ap.add_argument("--out", default=str(ROOT / "results" / "live"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_fh = open(out / f"{stamp}_raw.txt", "w", encoding="utf-8", buffering=1)   # line-buffered: kept even if the window is closed
    stream = Stream()
    live = Live(args, stream, out / f"{stamp}_bp.csv")
    print(live.describe())

    if args.replay:
        src, speed = open(args.replay, encoding="utf-8", errors="ignore"), args.speed
    else:
        try:
            import serial
            from serial.tools import list_ports
        except ImportError:
            sys.exit("pyserial is missing. Run: pip install pyserial")
        port = args.port
        if not port:
            ports = [p.device for p in list_ports.comports()]
            if len(ports) != 1:
                sys.exit(f"give --port (found: {', '.join(ports) or 'none'})")
            port = ports[0]
        try:
            ser = serial.Serial(port, args.baud, timeout=1)
        except serial.SerialException as e:
            found = ", ".join(f"{p.device} ({p.description})" for p in list_ports.comports()) or "none"
            if "FileNotFound" in str(e) or "cannot find" in str(e):
                sys.exit(f"{port} does not exist. Ports found: {found}. Is the board plugged in?")
            sys.exit(f"cannot open {port}: {e}. If it says access denied, close the PlatformIO monitor / "
                     f"dashboard first. Ports found: {found}")
        src, speed = iter(ser.readline, None), 0.0
        print(f"reading {port} at {args.baud} baud")
    threading.Thread(target=reader, args=(src, stream, raw_fh, speed), daemon=True).start()

    def estimates():
        while not stream.ended:
            time.sleep(args.step / (speed or 1.0))
            try:
                live.tick()
            except Exception as e:                      # keep running; show what went wrong
                print(f"  [estimate error] {e}", flush=True)

    threading.Thread(target=estimates, daemon=True).start()
    print("Rest the fingertip on the sensor. Commands: s (cuff starting) | 118/76 | c 118/76 | q\n", flush=True)
    try:
        for text in sys.stdin:
            if not live.command(text):
                break
    except KeyboardInterrupt:
        pass
    live.summary()
    raw_fh.close()
    live.fh.close()
    print(f"written {out / (stamp + '_raw.txt')} and {out / (stamp + '_bp.csv')}")


if __name__ == "__main__":
    main()
