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
  s          cuff started NOW. The screen goes quiet and waits for the reading.
  118/76     cuff reading: calibrates with the cleanest 30 s PPG window between
             30 s before "s" and now (cuff on the OTHER arm, so the PPG is not
             disturbed by inflation); without "s", the cleanest window of the
             last --entry-lag + 30 s. Prints the estimate just before it.
  x          cancel a waiting "s" (no reading coming)
  c 118/76   check only: compares with the estimate, no calibration
  q          quit and print the error summary

Signal quality: every estimate uses the cleanest of the 30 s windows ending in
the last --lookback seconds (default 10 s), so a short movement no longer
blanks 30 s of estimates. The quality limits themselves are the validated ones
(0.90 to calibrate, 0.80 to estimate) and are not lowered.

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

from bp_model.calibrated import MODEL_DIR, CalibratedBpModel, resample_25hz
from bp_model.features_v2 import extract_sensor
from bp_model.quality import ok_calibrate, ok_estimate

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
        self.pi = math.nan            # perfusion index (%) from the firmware's CSV row
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


def window_features(win):
    """The model's 26 features of a window (NaN when unusable)."""
    w = resample_25hz(win[0], win[1], 30.0)
    if w is None:
        return None
    with np.errstate(all="ignore"):
        return extract_sensor(w)


def best_window(stream, lo_ms, hi_ms, accept, step_ms=2500.0):
    """Cleanest window ending between lo_ms and hi_ms. Windows the model's quality gate
    accepts (accept(F) True) rank first, then by quality score.
    Returns (quality, accepted, end_ms, window); window None when there is none."""
    best, key = (float("nan"), False, None, None), None
    end = hi_ms
    while end >= lo_ms:
        win, why = stream.window(end)
        if win is not None:
            F = window_features(win)
            if F is not None and np.isfinite(F[-2]):
                k = (bool(accept(F)), float(F[-2]))
                if key is None or k > key:
                    key, best = k, (k[1], k[0], float(win[0][-1]), win)
        end -= step_ms
    return best


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
                stream.pi = float(parts[5])
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
        self.waiting = None           # device ms when "s" was typed, until the reading or "x"
        self.last_end = -1e18         # end of the last window given to the model (keeps time moving forward)
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
            pi = self.stream.pi
            extra = "" if math.isnan(pi) else f"  PI {pi:.1f}%"
            if not math.isnan(pi) and pi < 0.5:
                extra += " (weak pulse: warm the hand, rest the finger lightly, cover the sensor from light)"
            print(f"t={t_s:6.0f}s  [{st}] {hint}{extra}", flush=True)
            self.quiet = (st, end)
        self.row(end, "estimate", status=st)

    def tick(self):
        s = self.stream
        if s.resets != self.resets_seen:
            self.resets_seen = s.resets
            with self.mlock:
                self.model.reset()
            self.waiting = self.last_est = None
            self.last_end = -1e18
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
        t_s = (end - self.t0) / 1000
        if self.waiting is not None:                 # "s" typed: stay quiet until the reading
            waited = (end - self.waiting) / 1000
            if waited > 300:
                print("   no reading 5 min after s: waiting cancelled, estimates resume", flush=True)
                self.waiting = None
            else:
                return
        if not self.model.cuffs:
            win, why = s.window(end)
            self.status_line(end, t_s, "NEEDS_CALIBRATION" if win is not None else why,
                             "no cuff yet: type s when you start the cuff" if win is not None else "")
            return
        gate = self.model.gate
        q, passed, wend, win = best_window(s, max(end - self.args.lookback * 1000, self.last_end + 1), end,
                                           lambda F: ok_estimate(F, gate))
        min_q = gate.est_sqi
        if win is None or not passed:
            qtxt = "" if not np.isfinite(q) else f" (signal quality {q:.2f}, needs {min_q:.2f})"
            self.status_line(end, t_s, "POOR_SIGNAL" if win is not None or np.isfinite(q) else "COLLECTING",
                             "keep the finger still, light contact" + qtxt)
            return
        self.last_end = wend
        with self.mlock:
            r = self.model.estimate(*win)
        st = r["status"]
        if st in ("NEEDS_CALIBRATION", "COLLECTING", "POOR_SIGNAL"):
            self.status_line(end, t_s, st, "keep the finger still, light contact")
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
        if not math.isnan(s.pi):
            line += f"  PI {s.pi:.1f}%"
        line += f"  Q {q:.2f}"
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
            self.waiting = s.last_ms
            print("   waiting for the cuff reading (screen paused). Keep the sensor hand still;\n"
                  "   type the reading when the cuff shows it, e.g. 118/76  (x = cancel)", flush=True)
            self.row(s.last_ms, "cuff_start")
            return True
        if text == "x":
            if self.waiting is not None:
                self.waiting = None
                print("   cancelled; estimates resume", flush=True)
            return True
        m = CUFF.search(text)
        if not m:
            print("   commands: s | 118/76 | x | c 118/76 | q", flush=True)
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
        if self.waiting is not None:
            lo = self.waiting - 30000
            src = "cleanest window from 30 s before s until now"
        else:
            lo = s.last_ms - (self.args.entry_lag + 30) * 1000
            src = f"no s typed: cleanest window of the last {self.args.entry_lag + 30:g} s"
        self.waiting = None
        gate = self.model.gate
        q, _, t_cal, win = best_window(s, lo, s.last_ms, lambda F: ok_calibrate(F, gate))
        if win is None:
            print("   cannot calibrate (no complete 30 s window yet). Type s when the next cuff starts.", flush=True)
            self.row(s.last_ms, "cuff", cuff_sbp=sbp, cuff_dbp=dbp, result="COLLECTING")
            return True
        src += f", quality {q:.2f}"
        self.last_end = max(self.last_end, t_cal)
        motion = None if math.isnan(s.accel) else s.accel
        with self.mlock:
            ok, why = self.model.calibrate(win[0], win[1], sbp, dbp, motion_g=motion)
        print(f"   cuff {sbp:.0f}/{dbp:.0f} ({src}) -> {why}" + (f"; {before}" if before else ""), flush=True)
        if not ok:
            print(f"   not used: best signal quality was {q:.2f}, calibration needs "
                  f"{self.model.config.min_sqi:.2f}. Keep the sensor hand still (type with the other hand)"
                  " and take another cuff in a minute", flush=True)
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
    ap.add_argument("--lookback", type=float, default=10.0,
                    help="each estimate uses the cleanest 30 s window ending in the last this many seconds")
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
    print("Rest the fingertip on the sensor. Commands: s (cuff starting) | 118/76 | x (cancel s) | c 118/76 | q\n", flush=True)
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
