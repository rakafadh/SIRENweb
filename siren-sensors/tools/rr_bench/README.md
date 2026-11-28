# RR benchmark (host, no hardware)

All tools build against the firmware's own `respiration.cpp`.

**1. Synthetic benchmark** (finger placement, LED gain settling, rate change at
150 s, motion at 200-203 s; averages of 5 runs):

    g++ -O2 -std=c++17 -DNEWRR -Ishim -I../../src/ppg harness.cpp ../../src/ppg/respiration.cpp -o rr_bench
    ./rr_bench

**2. Mayer waves and fast breathing** (breathing plus a 0.1 Hz vessel-tone and
heart rate oscillation; 3 min, one line every 5 s):

    g++ -O2 -std=c++17 -Ishim -I../../src/ppg synth_check.cpp ../../src/ppg/respiration.cpp -o synth_check
    ./synth_check 15 1 0.3          # breaths/min, Mayer strength, breathing depth
    ./synth_check 45 0 0.6 110      # 4th number: heart rate (default 72)

**3. Replay a real recording** (`_raw.txt` from `tools/siren_monitor.py` or
`siren-bp-model-test/scripts/live_bp.py`; W lines = raw IR), with the
firmware's artefact and finger logic:

    g++ -O2 -std=c++17 -Ishim -I../../src/ppg replay_log.cpp ../../src/ppg/respiration.cpp -o replay_log
    ./replay_log path\to\20261128_180217_raw.txt rr_per_second.csv

The summary line gives the share of seconds with an RR (and how many were
slow path, tracking, fast) and why there was none. Breathe to a metronome or
count breaths during the recording, otherwise a replay shows availability,
not accuracy. Synthetic signals compare versions; they are not validation.

Results: RESULTS_v3.md (v2 vs v3), RESULTS_v4.md (v3 vs v4).
