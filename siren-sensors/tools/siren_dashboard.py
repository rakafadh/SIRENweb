#!/usr/bin/env python3
"""
SIREN - live monitoring dashboard

Reads the PPG node's CSV serial output, serves a browser dashboard, and logs
every sample to a CSV file for later analysis.

    pip install pyserial
    python tools/siren_dashboard.py --port COM13

Then open http://localhost:8000

The firmware must be emitting CSV. In src/ppg/main_ppg.cpp set:

    #define CSV_OUTPUT 1

This dashboard shows sensor readings only. No scoring, no interpretation.
"""

import argparse
import csv
import json
import math
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import triage as TR

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Run: pip install pyserial")

# --------------------------------------------------------------- constants ---
CSV_FIELDS = [
    "millis", "fs", "led", "ir_dc", "drift", "pi", "hr", "hr_src", "sdnn",
    "intervals", "spo2", "rr", "riiv", "riav", "rifv", "spread", "beats",
    "tobj", "tamb", "tcore", "accel_rms", "accel_peak", "gyro_rms",
]
HISTORY = 120               # samples retained for the sparklines, about 2 min
SENTINEL = -1.0             # firmware's "not available" marker

subscribers: "list[queue.Queue]" = []
subscribers_lock = threading.Lock()
latest: dict = {}
history: "deque[dict]" = deque(maxlen=HISTORY)

SERIES_KEYS = ("hr", "spo2", "rr", "tcore", "motion")

# Paramedic-entered values, edited from the browser. One patient at a time.
operator = TR.Operator()
# (seconds, NEWS2 total) pairs, used to measure deterioration.
score_history: "deque" = deque(maxlen=600)


# ------------------------------------------------------------------ parsing --
def to_float(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def clean(value, lo=None, hi=None):
    """Drop the firmware's -1 sentinel and anything outside a sane range."""
    if value is None:
        return None
    if value <= SENTINEL:
        return None
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    return value


def parse_line(line):
    parts = line.strip().split(",")
    if len(parts) != len(CSV_FIELDS):
        return None

    raw = dict(zip(CSV_FIELDS, parts))

    tcore = to_float(raw["tcore"])
    if tcore is not None and not (20.0 <= tcore <= 45.0):
        tcore = None

    return {
        "t": to_float(raw["millis"]) or 0.0,
        "wall": datetime.now().strftime("%H:%M:%S"),
        "hr": clean(to_float(raw["hr"]), 20, 250),
        "hr_src": raw["hr_src"],
        "sdnn": clean(to_float(raw["sdnn"]), 0, 500),
        "spo2": clean(to_float(raw["spo2"]), 50, 100),
        "rr": clean(to_float(raw["rr"]), 4, 50),
        "tcore": tcore,
        "tobj": to_float(raw["tobj"]),
        "tamb": to_float(raw["tamb"]),
        "motion": to_float(raw["accel_rms"]),
        "motion_peak": to_float(raw["accel_peak"]),
        "gyro": to_float(raw["gyro_rms"]),
        "pi": to_float(raw["pi"]),
        "fs": to_float(raw["fs"]),
        "ir": to_float(raw["ir_dc"]),
        "beats": to_float(raw["beats"]),
    }


def current_triage():
    """Run the two-track algorithm against the latest sample."""
    vitals = {
        "hr": latest.get("hr"),
        "spo2": latest.get("spo2"),
        "rr": latest.get("rr"),
        "temp": latest.get("tcore"),
        "pi": latest.get("pi"),
    }
    t = TR.classify(vitals, operator, list(score_history))
    return {
        "colour": t.name,
        "source": t.source,
        "news": t.news.total,
        "news_parts": t.news.parts,
        "news_measured": t.news.measured,
        "news_note": t.news.note,
        "news_colour": TR.COLOUR_NAME[t.news.colour],
        "start": TR.COLOUR_NAME.get(t.start.colour),
        "start_reason": t.start.reason,
        "delta": t.delta,
        "shock": t.shock,
        "alerts": t.alerts,
    }


def frame():
    return json.dumps({
        "latest": latest,
        "series": {k: [s[k] for s in history] for k in SERIES_KEYS},
        "triage": current_triage(),
        "operator": {
            "acvpu": operator.acvpu,
            "on_oxygen": operator.on_oxygen,
            "uncontrolled_bleeding": operator.uncontrolled_bleeding,
            "limb_loss_risk": operator.limb_loss_risk,
            "ambulatory": operator.ambulatory,
            "pain": operator.pain,
            "chest_pain": operator.chest_pain,
            "elderly": operator.elderly,
            "paediatric": operator.paediatric,
            "comorbid": operator.comorbid,
            "systolic": operator.systolic,
            "diastolic": operator.diastolic,
        },
    })


# ------------------------------------------------------------ serial reader --
def pick_port(requested):
    ports = list(list_ports.comports())
    if requested:
        return requested
    if not ports:
        sys.exit("No serial ports found. Plug in the PPG board.")
    if len(ports) == 1:
        print(f"Using the only port available: {ports[0].device}")
        return ports[0].device
    print("Several ports found. Choose one with --port:")
    for p in ports:
        print(f"  {p.device}  {p.description}")
    sys.exit(1)


def reader_thread(port, baud, logfile):
    writer = None
    handle = None
    if logfile:
        handle = open(logfile, "w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(["wall_time"] + CSV_FIELDS)
        print(f"Logging to {logfile}")

    warned_width = False

    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                print(f"Connected to {port} at {baud} baud.")
                while True:
                    try:
                        raw = ser.readline().decode("utf-8", errors="replace")
                    except serial.SerialException:
                        break
                    if not raw:
                        continue

                    # Skip the banner, the register dump and the CSV header;
                    # only numeric rows are of interest.
                    if not re.match(r"^\s*\d", raw):
                        continue

                    sample = parse_line(raw)
                    if sample is None:
                        n = len(raw.strip().split(","))
                        if n > 1 and not warned_width:
                            print(f"Ignoring rows with {n} columns, expected "
                                  f"{len(CSV_FIELDS)}. Reflash the PPG node "
                                  f"with the current firmware.")
                            warned_width = True
                        continue

                    if writer:
                        writer.writerow(
                            [datetime.now().isoformat(timespec="seconds")]
                            + raw.strip().split(",")
                        )
                        handle.flush()

                    history.append(sample)
                    latest.clear()
                    latest.update(sample)

                    # Record the NEWS2 total so deterioration can be measured
                    # over a real time window rather than a sample count.
                    n = TR.news2({"hr": sample["hr"], "spo2": sample["spo2"],
                                  "rr": sample["rr"], "temp": sample["tcore"]},
                                 operator)
                    score_history.append((sample["t"] / 1000.0, n.total))

                    payload = frame()
                    with subscribers_lock:
                        dead = []
                        for q in subscribers:
                            try:
                                q.put_nowait(payload)
                            except queue.Full:
                                dead.append(q)
                        for q in dead:
                            subscribers.remove(q)
        except serial.SerialException as exc:
            print(f"Serial error: {exc}. Retrying in 3 s.")
            time.sleep(3)


# -------------------------------------------------------------- web server ---
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass                                     # keep the console clean

    def do_POST(self):
        """Paramedic input from the browser. Only known fields are applied."""
        if self.path != "/input":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return

        for key, value in data.items():
            if not hasattr(operator, key):
                continue
            if key == "acvpu":
                if value in TR.ACVPU:
                    operator.acvpu = value
            elif key in ("pain", "systolic", "diastolic"):
                try:
                    setattr(operator, key, None if value in ("", None) else float(value))
                except (TypeError, ValueError):
                    pass
            else:
                setattr(operator, key, bool(value))

        body = frame().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

        # Push the recomputed triage to every open page, not just this one.
        with subscribers_lock:
            for q in subscribers:
                try:
                    q.put_nowait(body.decode("utf-8"))
                except queue.Full:
                    pass

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=8)
            with subscribers_lock:
                subscribers.append(q)

            try:
                if latest:
                    self.wfile.write(f"data: {frame()}\n\n".encode())
                    self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with subscribers_lock:
                    if q in subscribers:
                        subscribers.remove(q)
            return

        self.send_error(404)


# -------------------------------------------------------------------- page ---
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SIREN</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  /* Inter is the closest freely available analogue to SF Pro, so the layout
     looks the same on Windows as it would on a Mac. The native Apple faces
     are listed first and win wherever they exist. */
  :root {
    color-scheme: light;
    --plane:   #f5f5f7;
    --card:    #ffffff;
    --ink:     #1d1d1f;
    --ink-2:   #6e6e73;
    --ink-3:   #a1a1a6;
    --hair:    #e8e8ed;
    --hr:      #2a78d6;
    --spo2:    #eb6834;
    --rr:      #1baf7a;
    --temp:    #4a3aa7;
    --motion:  #6e6e73;
    --font: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
            "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif;
  }

  * { box-sizing: border-box; }
  html { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
  body {
    margin: 0;
    padding: 56px 24px 72px;
    background: var(--plane);
    color: var(--ink);
    font-family: var(--font);
    font-size: 17px;
    line-height: 1.47;
    letter-spacing: -0.01em;
  }
  .wrap { max-width: 1040px; margin: 0 auto; }

  header { margin-bottom: 36px; }
  h1 {
    font-size: 34px; font-weight: 600; margin: 0 0 4px;
    letter-spacing: -0.025em;
  }
  .sub { color: var(--ink-3); font-size: 15px; font-weight: 400; }
  .sub b { color: var(--ink-2); font-weight: 500; }

  .grid {
    display: grid; gap: 18px;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    margin-bottom: 18px;
  }
  .card {
    background: var(--card);
    border-radius: 20px;
    padding: 26px 26px 20px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.04);
  }
  .label {
    font-size: 14px; font-weight: 500; color: var(--ink-2);
    margin: 0 0 14px; letter-spacing: -0.005em;
  }
  .val { display: flex; align-items: baseline; gap: 7px; }
  .num {
    font-size: 58px; font-weight: 300; line-height: 0.95;
    letter-spacing: -0.035em; font-variant-numeric: tabular-nums;
  }
  .num.dim { color: var(--ink-3); font-weight: 200; }
  .unit { font-size: 17px; font-weight: 400; color: var(--ink-3); }
  .note { font-size: 13px; color: var(--ink-3); margin-top: 10px; min-height: 18px; }

  svg.spark { display: block; width: 100%; height: 40px; margin-top: 16px; }

  .wide { grid-column: 1 / -1; }
  .motion-row { display: flex; align-items: flex-end; gap: 40px; flex-wrap: wrap; }
  .motion-row > div { flex: 0 0 auto; }
  .motion-spark { flex: 1 1 320px; min-width: 240px; }
  .motion-spark svg { height: 52px; margin-top: 0; }
  .num.sm { font-size: 30px; font-weight: 400; letter-spacing: -0.02em; }

  .strip {
    display: flex; gap: 36px; flex-wrap: wrap;
    padding: 20px 26px;
    background: var(--card); border-radius: 20px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.04);
  }
  .banner {
    display: flex; align-items: center; gap: 22px; flex-wrap: wrap;
    border-radius: 20px; padding: 24px 26px; margin-bottom: 18px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.04);
    background: var(--card); border-left: 6px solid var(--ink-3);
  }
  .banner.Merah  { border-left-color: #d03b3b; }
  .banner.Kuning { border-left-color: #fab219; }
  .banner.Hijau  { border-left-color: #0ca30c; }
  .banner .colour { font-size: 40px; font-weight: 500; letter-spacing: -0.03em; }
  .banner.Merah  .colour { color: #a32d2d; }
  .banner.Kuning .colour { color: #8a5f00; }
  .banner.Hijau  .colour { color: #0a7a0a; }
  .banner .tracks { display: flex; gap: 12px; flex-wrap: wrap; }
  .track {
    border: 1px solid var(--hair); border-radius: 12px;
    padding: 12px 18px; min-width: 112px; background: transparent;
  }
  .track span { display: block; font-size: 13px; color: var(--ink-3); margin-bottom: 4px; }
  .track b { font-size: 20px; font-weight: 500; letter-spacing: -0.02em; }
  .track b.Merah  { color: #a32d2d; }
  .track b.Kuning { color: #8a5f00; }
  .track b.Hijau  { color: #0a7a0a; }

  .banner .metrics { display: flex; gap: 28px; margin-left: auto; flex-wrap: wrap; }
  .banner .metrics div { font-size: 15px; }
  .banner .metrics span { display: block; font-size: 13px; color: var(--ink-3); }

  .alerts { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; }
  .alerts div {
    border: 1px solid var(--hair); border-radius: 10px;
    padding: 9px 14px; font-size: 14px; color: var(--ink-2);
    background: transparent;
  }

  .inputs { display: grid; gap: 18px 26px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
  .inputs label { font-size: 14px; color: var(--ink-2); display: block; margin-bottom: 6px; }
  .inputs select, .inputs input[type=number] {
    font-family: var(--font); font-size: 16px; padding: 8px 10px; width: 100%;
    border: 1px solid var(--hair); border-radius: 10px; background: var(--card);
    color: var(--ink);
  }
  .checks { display: flex; flex-direction: column; gap: 8px; }
  .checks label {
    display: flex; align-items: center; gap: 9px; margin: 0;
    font-size: 15px; color: var(--ink); cursor: pointer;
  }
  .checks input { width: 17px; height: 17px; accent-color: #2a78d6; }
  .strip div { font-size: 15px; }
  .strip span { display: block; font-size: 13px; color: var(--ink-3); margin-bottom: 2px; }
  .strip b { font-weight: 500; font-variant-numeric: tabular-nums; }

  /* The input panel folds away, because on a 7 inch panel it costs more
     vertical space than the vitals it sits below. */
  details.card > summary {
    cursor: pointer; list-style: none; user-select: none;
    display: flex; align-items: center; gap: 8px; margin: 0;
  }
  details.card > summary::-webkit-details-marker { display: none; }
  details.card > summary::after {
    content: "\25BE"; font-size: 13px; color: var(--ink-3);
    transition: transform 0.15s;
  }
  details.card[open] > summary::after { transform: rotate(180deg); }
  details.card[open] > summary { margin-bottom: 16px; }

  /* ------------------------------------------------- 7 inch panels ----
     Triggered on viewport height, because the constraint on a 1024x600 or
     800x480 LCD is vertical. Everything shrinks proportionally so the
     triage colour and the four vitals stay above the fold without
     scrolling, which is the only thing that matters at a glance. */
  @media (max-height: 700px) {
    body { padding: 12px 14px 12px; font-size: 15px; }
    .wrap { max-width: none; }
    header { margin-bottom: 8px; }
    h1 { font-size: 20px; }
    .sub { font-size: 13px; }

    .grid { gap: 9px; margin-bottom: 9px; }
    .card { padding: 12px 16px 8px; border-radius: 14px; }
    .label { font-size: 12px; margin-bottom: 6px; }
    .num { font-size: 34px; }
    .num.sm { font-size: 22px; }
    .unit { font-size: 13px; }
    .note { font-size: 11px; margin-top: 4px; min-height: 13px; }
    svg.spark { height: 22px; margin-top: 6px; }
    .motion-spark svg { height: 28px; }

    .banner { padding: 12px 16px; margin-bottom: 9px; border-left-width: 5px; }
    .banner .colour { font-size: 28px; }
    .banner .tracks { gap: 8px; }
    .track { padding: 7px 12px; min-width: 88px; border-radius: 10px; }
    .track span { font-size: 11px; margin-bottom: 2px; }
    .track b { font-size: 16px; }
    .banner .metrics { gap: 18px; }
    .banner .metrics div { font-size: 14px; }
    .banner .metrics span { font-size: 11px; }
    .alerts { margin-top: 8px; gap: 7px; }
    .alerts div { padding: 5px 10px; font-size: 12px; border-radius: 8px; }

    .strip { padding: 10px 16px; gap: 18px; border-radius: 14px; }
    .strip div { font-size: 13px; }
    .strip span { font-size: 11px; }
    details.card[open] > summary { margin-bottom: 12px; }
  }

  /* Narrow panels, 800 px and under. Shrink the minimum card width so the
     four vitals still fit on one row instead of wrapping to two. */
  @media (max-width: 880px) {
    .grid { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
    .inputs { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px 16px; }
    .motion-row { gap: 20px; }
    .motion-spark { flex-basis: 180px; min-width: 150px; }
  }

  /* Touch targets. A 7 inch panel is usually a touchscreen, and the default
     17 px checkbox is too small to hit reliably in a moving vehicle. */
  @media (pointer: coarse) {
    .checks input { width: 24px; height: 24px; }
    .checks label { gap: 12px; padding: 4px 0; }
    .inputs select, .inputs input[type=number] { font-size: 17px; padding: 11px 12px; }
    details.card > summary { padding: 6px 0; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>SIREN</h1>
    <div class="sub">Patient pod 1 &nbsp;&middot;&nbsp; <b id="clock">waiting for data</b></div>
  </header>

  <div class="banner" id="banner">
    <div>
      <p class="label" style="margin-bottom:4px">Kategori triase</p>
      <div class="colour" id="t-colour">--</div>
    </div>
    <div class="tracks">
      <div class="track"><span>START</span><b id="t-start">--</b></div>
      <div class="track"><span>NEWS2</span><b id="t-newscol">--</b></div>
    </div>
    <div class="metrics">
      <div><span>Skor NEWS2</span><b id="t-news">--</b></div>
      <div><span>&Delta;NEWS2 60 s</span><b id="t-delta">--</b></div>
      <div><span>Shock index</span><b id="t-shock">--</b></div>
    </div>
    <div class="alerts" id="t-alerts" style="flex-basis:100%"></div>
  </div>

  <div class="grid">
    <div class="card" data-k="hr">
      <p class="label">Heart rate</p>
      <div class="val"><span class="num dim">--</span><span class="unit">bpm</span></div>
      <div class="note"></div>
      <svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none">
        <polyline fill="none" stroke="var(--hr)" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" points=""/>
      </svg>
    </div>

    <div class="card" data-k="spo2">
      <p class="label">Oxygen saturation</p>
      <div class="val"><span class="num dim">--</span><span class="unit">%</span></div>
      <div class="note"></div>
      <svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none">
        <polyline fill="none" stroke="var(--spo2)" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" points=""/>
      </svg>
    </div>

    <div class="card" data-k="rr">
      <p class="label">Respiration</p>
      <div class="val"><span class="num dim">--</span><span class="unit">/min</span></div>
      <div class="note"></div>
      <svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none">
        <polyline fill="none" stroke="var(--rr)" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" points=""/>
      </svg>
    </div>

    <div class="card" data-k="temp">
      <p class="label">Temperature</p>
      <div class="val"><span class="num dim">--</span><span class="unit">&deg;C</span></div>
      <div class="note"></div>
      <svg class="spark" viewBox="0 0 300 40" preserveAspectRatio="none">
        <polyline fill="none" stroke="var(--temp)" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" points=""/>
      </svg>
    </div>

    <div class="card wide">
      <p class="label">Vehicle motion</p>
      <div class="motion-row">
        <div>
          <div class="val">
            <span class="num sm dim" id="m-rms">--</span><span class="unit">g rms</span>
          </div>
          <div class="note" id="m-note"></div>
        </div>
        <div>
          <div class="val">
            <span class="num sm dim" id="m-peak">--</span><span class="unit">g peak</span>
          </div>
          <div class="note">largest jolt this second</div>
        </div>
        <div>
          <div class="val">
            <span class="num sm dim" id="m-gyro">--</span><span class="unit">&deg;/s</span>
          </div>
          <div class="note">rotation</div>
        </div>
        <div class="motion-spark">
          <svg class="spark" viewBox="0 0 300 52" preserveAspectRatio="none">
            <polyline fill="none" stroke="var(--motion)" stroke-width="2"
                      stroke-linejoin="round" stroke-linecap="round" points=""
                      id="m-spark"/>
          </svg>
        </div>
      </div>
    </div>
  </div>

  <details class="card" id="inputcard" style="margin-bottom:18px">
    <summary class="label">Input petugas</summary>
    <div class="inputs">
      <div>
        <label for="i-acvpu">Kesadaran (ACVPU)</label>
        <select id="i-acvpu" data-k="acvpu">
          <option value="alert">Alert</option>
          <option value="confusion">Confusion (baru)</option>
          <option value="voice">Voice</option>
          <option value="pain">Pain</option>
          <option value="unresponsive">Unresponsive</option>
        </select>
      </div>
      <div>
        <label for="i-sys">Sistolik (mmHg)</label>
        <input type="number" id="i-sys" data-k="systolic" min="40" max="260" step="1">
      </div>
      <div>
        <label for="i-dia">Diastolik (mmHg)</label>
        <input type="number" id="i-dia" data-k="diastolic" min="20" max="200" step="1">
      </div>
      <div>
        <label for="i-pain">Nyeri (0-10)</label>
        <input type="number" id="i-pain" data-k="pain" min="0" max="10" step="1">
      </div>
      <div class="checks">
        <label><input type="checkbox" data-k="on_oxygen"> Oksigen tambahan</label>
        <label><input type="checkbox" data-k="ambulatory"> Dapat berjalan</label>
        <label><input type="checkbox" data-k="chest_pain"> Nyeri dada</label>
      </div>
      <div class="checks">
        <label><input type="checkbox" data-k="uncontrolled_bleeding"> Pendarahan tak terkendali</label>
        <label><input type="checkbox" data-k="limb_loss_risk"> Risiko kehilangan anggota gerak</label>
      </div>
      <div class="checks">
        <label><input type="checkbox" data-k="elderly"> Lansia</label>
        <label><input type="checkbox" data-k="paediatric"> Balita</label>
        <label><input type="checkbox" data-k="comorbid"> Komorbiditas</label>
      </div>
    </div>
  </details>

  <div class="strip">
    <div><span>Perfusion index</span><b id="q-pi">--</b></div>
    <div><span>Sample rate</span><b id="q-fs">--</b></div>
    <div><span>IR level</span><b id="q-ir">--</b></div>
    <div><span>Beats detected</span><b id="q-beats">--</b></div>
    <div><span>Heart rate variability</span><b id="q-sdnn">--</b></div>
    <div><span>Skin / ambient</span><b id="q-temps">--</b></div>
  </div>

</div>

<script>
const SERIES_KEY = { hr:"hr", spo2:"spo2", rr:"rr", temp:"tcore" };
const DP = { hr:0, spo2:0, rr:1, temp:1 };

function fmt(v, dp) {
  if (v === null || v === undefined) return "--";
  return Number(v).toFixed(dp);
}

function sparkPoints(values, h) {
  const pts = values.map((v, i) => [i, v]).filter(p => p[1] !== null);
  if (pts.length < 2) return "";
  const ys = pts.map(p => p[1]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  const n = values.length - 1 || 1;
  const top = 4, bottom = h - 4;
  return pts.map(([i, v]) => {
    const x = (i / n) * 300;
    const y = bottom - ((v - lo) / (hi - lo)) * (bottom - top);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function setNum(el, value, dp) {
  el.textContent = value === null ? "--" : fmt(value, dp);
  el.classList.toggle("dim", value === null);
}

function render(msg) {
  const d = msg.latest, s = msg.series;
  document.getElementById("clock").textContent = "live " + d.wall;

  for (const k of ["hr", "spo2", "rr", "temp"]) {
    const card = document.querySelector(`.card[data-k="${k}"]`);
    const value = k === "temp" ? d.tcore : d[k];
    setNum(card.querySelector(".num"), value, DP[k]);
    card.querySelector("polyline").setAttribute(
      "points", sparkPoints(s[SERIES_KEY[k]] || [], 40));
  }

  // Per-card secondary readings, kept to one quiet line of plain text.
  document.querySelector('.card[data-k="hr"] .note').textContent =
    d.hr === null ? "" :
    (d.hr_src === "b" ? "from beat timing" : "from Maxim algorithm");
  document.querySelector('.card[data-k="spo2"] .note').textContent =
    d.spo2 === null ? "" : "uncalibrated";
  document.querySelector('.card[data-k="rr"] .note').textContent =
    d.rr === null ? "needs 30 s of contact" : "fused from 3 PPG sources";
  document.querySelector('.card[data-k="temp"] .note').textContent =
    d.tcore === null ? "" :
    (d.tobj === null ? "" : "skin " + fmt(d.tobj, 1) + " °C");

  setNum(document.getElementById("m-rms"), d.motion, 3);
  setNum(document.getElementById("m-peak"), d.motion_peak, 3);
  setNum(document.getElementById("m-gyro"), d.gyro, 1);
  document.getElementById("m-spark").setAttribute(
    "points", sparkPoints(s.motion || [], 52));

  // Descriptive, not prescriptive: the number is the measurement, this is
  // only a plain-language reading of its scale.
  const m = d.motion;
  document.getElementById("m-note").textContent =
    m === null ? "no sensor" :
    m < 0.02 ? "stationary" :
    m < 0.08 ? "light movement" :
    m < 0.20 ? "moderate vibration" : "heavy vibration";

  document.getElementById("q-pi").textContent = fmt(d.pi, 2) + " %";
  document.getElementById("q-fs").textContent = fmt(d.fs, 1) + " Hz";
  document.getElementById("q-ir").textContent =
    d.ir === null ? "--" : Math.round(d.ir).toLocaleString();
  document.getElementById("q-beats").textContent = fmt(d.beats, 0);
  document.getElementById("q-sdnn").textContent =
    d.sdnn === null ? "--" : fmt(d.sdnn, 1) + " ms";
  document.getElementById("q-temps").textContent =
    (d.tobj === null ? "--" : fmt(d.tobj, 1)) + " / " +
    (d.tamb === null ? "--" : fmt(d.tamb, 1)) + " °C";

  if (msg.triage) renderTriage(msg.triage, msg.operator);
}

let suppressEcho = false;

function renderTriage(t, op) {
  const b = document.getElementById("banner");
  b.className = "banner " + (t.colour || "");
  document.getElementById("t-colour").textContent = t.colour || "--";

  const startEl = document.getElementById("t-start");
  startEl.textContent = t.start || "--";
  startEl.className = t.start || "";

  const newsEl = document.getElementById("t-newscol");
  newsEl.textContent = t.news_colour || "--";
  newsEl.className = t.news_colour || "";

  document.getElementById("t-news").textContent =
    t.news === null ? "--" : t.news + " / " + t.news_measured + " param";
  document.getElementById("t-delta").textContent =
    t.delta === null ? "--" : (t.delta > 0 ? "+" : "") + t.delta;
  document.getElementById("t-shock").textContent =
    t.shock === null ? "--" : Number(t.shock).toFixed(2);

  document.getElementById("t-alerts").innerHTML =
    (t.alerts || []).map(a => "<div>" + a + "</div>").join("");

  // Mirror server state into the form, without firing another POST.
  if (!op) return;
  suppressEcho = true;
  document.querySelectorAll("[data-k]").forEach(el => {
    const v = op[el.dataset.k];
    if (el.type === "checkbox") el.checked = !!v;
    else if (document.activeElement !== el) el.value = (v === null || v === undefined) ? "" : v;
  });
  suppressEcho = false;
}

function pushInput(el) {
  if (suppressEcho) return;
  const body = {};
  body[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
  fetch("/input", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  }).then(r => r.json()).then(m => renderTriage(m.triage, m.operator));
}

document.querySelectorAll("[data-k]").forEach(el => {
  el.addEventListener("change", () => pushInput(el));
});

const es = new EventSource("/stream");
es.onmessage = e => render(JSON.parse(e.data));
es.onerror = () => {
  document.getElementById("clock").textContent = "disconnected";
};
</script>
</body>
</html>
"""


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description="SIREN live dashboard")
    ap.add_argument("--port", help="serial port, e.g. COM13 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--log", default="siren_log.csv",
                    help="CSV log file, or empty string to disable")
    args = ap.parse_args()

    port = pick_port(args.port)

    t = threading.Thread(
        target=reader_thread, args=(port, args.baud, args.log or None), daemon=True)
    t.start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    print(f"Dashboard at http://localhost:{args.http_port}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()