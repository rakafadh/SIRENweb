# RR v3 vs v2 (28 Nov 2026)

Why v3: at rest the strongest slow component of RIIV and RIFV was the Mayer wave
(0.09-0.12 Hz, 6-7 per min). v2 searched down to 6 per min and locked onto it,
so it only worked with deep breathing. v3 searches 8.4-36 per min with a
spectral peak, fuses any two agreeing signals, and has a separate check for slow
breathing below 8.4 per min.

## Synthetic benchmark (harness.cpp)

```
v2
25Hz, gain in range        first= 19.1s  valid= 80%  wrong(>4)= 0.0%  rmse= 0.10  track_change=  18.1s  after_motion=  26.7s
10Hz, gain in range        first= 38.6s  valid= 59%  wrong(>4)= 0.0%  rmse= 0.23  track_change=  20.7s  after_motion=  30.3s
10Hz, gain must settle     first= 40.4s  valid= 50%  wrong(>4)= 0.2%  rmse= 0.38  track_change=  27.5s  after_motion=  40.1s
10Hz, slow breathing 8     first= 34.2s  valid= 71%  wrong(>4)= 0.0%  rmse= 0.07  track_change=  18.1s  after_motion=  36.7s
10Hz, fast breathing 28    first=124.5s  valid=  9%  wrong(>4)= 0.0%  rmse= 0.83  track_change=  41.1s  after_motion= 230.0s
25Hz, gain must settle     first= 25.1s  valid= 77%  wrong(>4)= 0.0%  rmse= 0.11  track_change=  20.3s  after_motion=  27.3s
25Hz, slow breathing 8     first= 19.1s  valid= 81%  wrong(>4)= 0.0%  rmse= 0.09  track_change=  17.9s  after_motion=  24.7s
25Hz, fast breathing 28    first= 28.9s  valid= 44%  wrong(>4)= 1.9%  rmse= 0.87  track_change=  24.1s  after_motion=  24.7s

v3
25Hz, gain in range        first= 19.1s  valid= 81%  wrong(>4)= 0.0%  rmse= 0.06  track_change=  18.1s  after_motion=  25.1s
10Hz, gain in range        first= 31.6s  valid= 80%  wrong(>4)= 0.0%  rmse= 0.14  track_change=  18.5s  after_motion=   0.7s
10Hz, gain must settle     first= 31.6s  valid= 78%  wrong(>4)= 0.0%  rmse= 0.13  track_change=  19.1s  after_motion=   0.7s
10Hz, slow breathing 8     first= 30.8s  valid= 86%  wrong(>4)= 0.0%  rmse= 0.36  track_change=  17.7s  after_motion=   0.7s
10Hz, fast breathing 28    first= 42.8s  valid= 74%  wrong(>4)= 0.0%  rmse= 0.16  track_change=  18.5s  after_motion=   5.9s
25Hz, gain must settle     first= 23.9s  valid= 79%  wrong(>4)= 0.0%  rmse= 0.08  track_change=  18.1s  after_motion=  24.5s
25Hz, slow breathing 8     first= 19.1s  valid= 81%  wrong(>4)= 0.0%  rmse= 0.07  track_change=  18.1s  after_motion=  24.7s
25Hz, fast breathing 28    first= 19.7s  valid= 81%  wrong(>4)= 0.0%  rmse= 0.11  track_change=  18.1s  after_motion=  24.1s
```

## Mayer waves + shallow breathing (synth_check.cpp, 2 min each)

| True rate, case | v2 valid | v2 within 2/min | v3 valid | v3 within 2/min |
|---|---|---|---|---|
| 15/min, Mayer, shallow | 89% | 0% | 100% | 100% |
| 12/min, Mayer, shallow | 11% | 0% | 100% | 79% |
| 18/min, Mayer, shallow | 21% | 0% | 100% | 100% |
| 10/min, Mayer, half depth | 26% | 16% | 89% | 89% |
| 8/min, normal depth | 100% | 100% | 100% | 100% |
| 6/min, deep | 74% | 74% | 100% | 100% |

v2 at 15/min reproduces the symptom seen on the device: an output 89% of the time, never the true rate.

## Real recordings (replay_log.cpp, MAX30102 at 25 Hz, seated, no counted reference)

| Recording | v2 valid | v2 median (p10-p90) | v3 valid | v3 median (p10-p90) |
|---|---|---|---|---|
| 28 Nov 15:05, 19 min | 29% | 8.1 (6.5-9.2) | 60% | 11.4 (9.2-17.6) |
| 28 Nov 15:00, 4 min | 51% | 7.4 (6.6-24.3) | 58% | 23.7 (14.1-25.6) |
| 25 Nov 11:22, 7 min | 24% | 7.3 (6.7-8.3) | 54% | 10.8 (9.1-14.1) |
| 24 Nov 21:18, 6 min | 40% | 7.6 (6.5-9.1) | 45% | 18.6 (10.8-21.5) |

The v2 replay matches what the device printed on 28 Nov 15:05 (valid 30%, median 8),
so the replay reproduces the firmware. v2's outputs sit in the Mayer band on every
recording; v3's sit in the normal resting range. Without a counted breathing rate
these recordings show availability, not accuracy: the next test needs one.
