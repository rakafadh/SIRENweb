# RR v4 vs v3 (28 Nov 2026)

v4 changes: smoothing reset after a gap (and the stale-slot bug fixed), Mayer
guard, one-signal tracking, the firmware's reason sent on an R line, and no
fixed 36/min ceiling (search up to half the heart rate, 8 Hz grid with a 3 Hz
anti-alias filter, alias checks for fast breathing). See respiration.h.

## Real recordings (replay_log.cpp, MAX30102 25 Hz, seated, no counted reference)

| Recording | v3 RR shown | v4 RR shown | of which tracking | v4 median (p10-p90) |
|---|---|---|---|---|
| 28 Nov 18:02, 7 min | 38% | 73% | 29% | 11.9 (9.5-18.6) |
| 28 Nov 15:05, 19 min | 60% | 80% | 20% | 11.5 (9.4-17.5) |
| 28 Nov 15:00, 4 min | 58% | 78% | 4% | 23.3 (12.9-25.5) |
| 25 Nov 11:22, 7 min | 54% | 75% | 20% | 11.1 (9.2-15.4) |
| 24 Nov 21:18, 6 min | 45% | 67% | 14% | 18.4 (10.9-21.5) |
| 28 Nov 17:54, 1 min | 18% | 45% | 23% | 11.1 (9.7-11.3) |

Medians barely move: v4 shows RR more often, it does not change what it shows.
"fast?" (beats missed) fired 0 times on these recordings.
In the 18:02 recording, outputs that matched none of the three signals fell
from 11 to 4; the 4 left are the normal one-second lag of the median at a
change of rate.

## Synthetic benchmark (harness.cpp, 5 runs each)

| Scenario | v3 valid | v4 valid | wrong (>4/min) v3 / v4 |
|---|---|---|---|
| 25 Hz, gain in range | 81% | 85% | 0 / 0 |
| 10 Hz, gain in range | 80% | 88% | 0 / 0 |
| 10 Hz, gain must settle | 78% | 86% | 0 / 0 |
| 10 Hz, slow 8 | 86% | 87% | 0 / 0 |
| 10 Hz, fast 28 | 74% | 80% | 0 / 0 |
| 25 Hz, gain must settle | 79% | 83% | 0 / 0 |
| 25 Hz, slow 8 | 81% | 85% | 0 / 0 |
| 25 Hz, fast 28 | 81% | 84% | 0 / 0 |

Recovery after motion at 25 Hz: about 24 s (v3) to about 16 s (v4).

## Synthetic, Mayer waves and fast breathing (synth_check.cpp, 3 min each)

| True rate, case, HR | v3 within 2/min | v3 wrong | v4 within 2/min | v4 wrong |
|---|---|---|---|---|
| 15, Mayer, shallow, 72 | 100% | 0% | 100% | 0% |
| 12, Mayer, shallow, 72 | 87% | 13% | 90% | 10% |
| 10, Mayer, half depth, 72 | 87% | 0% | 94% | 0% |
| 9, Mayer, 72 | 87% | 0% | 100% | 0% |
| 6, deep slow, 72 | 100% | 0% | 100% | 0% |
| 36, HR 100 | 0% | 45% | 100% | 0% |
| 40, HR 100 | 0% | 100% (shows 20) | 100% | 0% |
| 45, Mayer, HR 110 | 0% | 55% | 100% | 0% |
| 50, half depth, HR 120 | 0% | 23% | 100% | 0% |
| 60, half depth, HR 140 | 0% | 90% (shows 20) | 100% | 0% |
| 40, HR 80 | 0% | 19% | 58% | 0% |
| 50, full depth, HR 120 | 0% | 97% (shows 20) | 0% | 0% (blocked, "fast?") |
| 60, full depth, HR 140 | 0% | 84% (shows 20) | 0% | 0% (blocked, "fast?") |

The last two rows: deep fast breathing makes the beat detector miss every
other beat (HR reads 60 instead of 120). v4 detects this and shows no RR
("fast?", count manually) instead of 20. The HR on the display is still wrong
in that case; the beat detector needs its own fix.
The 12/min case: the wrong 10% are the slow-breathing path accepting the
synthetic Mayer wave at 6/min (as in v3).
