# RR benchmark (host, no hardware)

Simulates a MAX30102 PPG stream (finger placement, LED gain settling, respiratory
baseline/amplitude/RSA modulation, a rate change at 150 s, motion at 200-203 s) and
runs the same loop logic as `main_ppg.cpp` around `RespirationMonitor`.

    g++ -O2 -std=c++17 -DNEWRR -Ishim -I../../src/ppg harness.cpp ../../src/ppg/respiration.cpp -o rr_bench
    ./rr_bench

Columns: time to first RR after finger placement, share of seconds with a valid RR,
share of valid RRs off by more than 4 breaths/min, RMSE, time to follow a rate change
to within 2 breaths/min, time to recover after motion. Averages of 5 runs.
The signal is synthetic: use it to compare versions, not as clinical validation.
