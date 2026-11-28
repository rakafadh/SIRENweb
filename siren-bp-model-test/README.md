# SIREN BP model, standalone test kit

The blood-pressure model from SIRENweb, separated from siren_app so it can be
tested on its own. No FastAPI, no database, no serial port. It runs the same
code siren_app runs, so a result here is what the app would show.

## File structure

```
siren-bp-model-test/
├── README.md
├── requirements.txt          pinned to scikit-learn 1.5.2 (the version the model was saved with)
├── model/
│   ├── bp_model.joblib       the trained random forest (copied from siren_app/models)
│   └── bp_model_metrics.json stored test metrics + PPG rescaling reference
├── bp_model/                 the model as a small Python package
│   ├── features.py           36 features, copied verbatim (signal_summary_v1)
│   ├── preprocess.py         sensor window -> 125 Hz model input; serial log parser
│   ├── predictor.py          BpModel class, NEWS2 SBP score
│   ├── features_v2.py        NEW  26 pulse-shape features at 25 Hz (morph25_v1)
│   ├── calibrated.py         NEW  cuff-calibrated model + calibration rules + alerts
│   ├── multipoint.py         NEW  multi-point calibration (several cuff readings)
│   ├── kalman.py             NEW  Kalman fusion of cuff + PPG, uncertainty, cautious NEWS2 score
│   ├── pat.py                NEW  pulse arrival time from ECG lead II + PPG (R peaks, foot, upstroke)
│   ├── respvar.py            NEW  breathing variation of the PPG (pleth variability analogue)
│   ├── evaluation.py         NEW  shared accuracy and alert metrics
│   └── synthetic.py          fake PPG for smoke tests only
├── scripts/
│   ├── inspect_model.py      step 1  what is inside the model
│   ├── smoke_test.py         step 2  does it run, does the output move
│   ├── eval_mimic.py         step 3  accuracy on MIMIC-BP (3 modes)
│   ├── run_on_log.py         step 4  run on a real SIREN serial log
│   ├── retrain_calibrated.py NEW  step 5  retrain + compare ML models
│   ├── run_calibrated_on_log.py NEW step 6  calibrated model on a real log
│   ├── eval_multipoint.py    NEW  step 7  multi-point calibration + alert thresholds
│   ├── vitaldb_download.py   NEW  step 8  download VitalDB cases -> features
│   ├── vitaldb_train.py      NEW  step 9  time-ordered training, cuff-gap test
│   ├── robustness_check.py   NEW  step 10 confidence intervals on the NEWS2 check
│   ├── vitaldb_pat_download.py NEW step 11 ECG + PPG at 250 Hz -> PAT features
│   ├── vitaldb_pat_train.py  NEW  step 12 PPG only vs PPG + PAT
│   ├── vitaldb_resp_download.py NEW step 13 PLETH at 100 Hz -> breathing variation features
│   ├── live_bp.py            NEW  step 14 live BP from the SIREN PPG board, cuff readings typed in
│   ├── quality_gate_check.py NEW  step 15 choose and check the rescue quality gate on VitalDB
│   └── make_sample_data.py   rebuilds sample_data/
├── tests/
│   └── test_bp_model.py      pytest checks
├── sample_data/
│   ├── sample_serial_log.txt synthetic log in the firmware's W-line format
│   └── fake_mimic/           3+3 synthetic subjects, same layout as the real dataset
├── vitaldb_cache/            one small .npz per VitalDB case (made by step 8)
└── results/                  outputs land here (CSV, JSON, Bland–Altman PNG)
```

## Setup (Windows PowerShell)

```
cd siren-bp-model-test
py -3.12 -m venv .venv            # Python 3.10 to 3.13
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked, run once
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Run, in order

| Step | Command | What it tells you |
|---|---|---|
| 0 | `python -m pytest -q` | 15 checks pass, so the kit is set up correctly |
| 1 | `python scripts/inspect_model.py` | model type, settings, feature importance, stored metrics |
| 2 | `python scripts/smoke_test.py` | output on very different synthetic pulses, which features are frozen live |
| 3 | `python scripts/eval_mimic.py --dataset "E:/Main Room/UI KEOS (was ITB)/Tingkat Akhir/Despro/dataset"` | real accuracy, see below |
| 4 | `python scripts/run_on_log.py my_log.txt --cuff 118/76` | estimates on your own MAX30102 recording vs a cuff |

Try step 3 and 4 on the sample data first.

```
python scripts/eval_mimic.py --dataset sample_data/fake_mimic
python scripts/run_on_log.py sample_data/sample_serial_log.txt --cuff 120/80
```

Sample data is synthetic. Its numbers only show the scripts work.

## Step 3 modes

- **patient**: averages each patient's windows, exactly like training. Should
  reproduce `bp_model_metrics.json` (SBP MAE 9.4, SD 12.15).
- **window**: one estimate per 30 s window, which is how siren_app uses it.
- **sensor**: each MIMIC window cut to 25 Hz and flipped like MAX30102 IR, then
  sent through the app's preprocessing. The gap between `window` and `sensor`
  is the cost of the sensor's lower sample rate.

Each mode prints ME, SD, MAE, r, Bland–Altman limits, ISO 81060-2 criterion 1
(PASS needs |ME| ≤ 5 and SD ≤ 8), NEWS2 SBP band agreement, and how many
SBP ≤ 100 cases it catches. Options `--split val`, `--max-windows 50`,
`--sensor-fs 10`.

## Recording a log for step 4

1. Flash firmware that prints the raw waveform (`W,...` lines, `WAVEFORM_OUTPUT 1`).
2. In the PlatformIO serial monitor, sit still with a finger on the sensor for at least 60 s.
3. Take a cuff reading during the recording.
4. Easiest is to let PlatformIO save it: `pio device monitor -e ppg -f log2file`
   writes a `platformio-device-monitor-*.log` file in the siren-sensors folder.
   Copying the monitor text into a .txt file also works.
5. `python scripts/run_on_log.py my_log.txt --cuff 118/76`

Repeat at rest and after exercise. If the estimate barely moves while the cuff
changes by 20 mmHg or more, the model is not tracking BP on this sensor.

## Known limits, kept on purpose

- `features.py` keeps the `np.linspace(min, max)` "downsample" bug, because the
  saved model was trained with it. Fix it only together with retraining.
- Predictions run single-threaded so repeated runs give identical numbers.

## Step 5, the calibrated model (new)

```
python scripts/retrain_calibrated.py --dataset "D:/dataverse_files"
```

Takes about 10 to 20 min on a laptop. The first run extracts features from all
45,720 windows and caches them in `results/features_morph25.npz`; later runs
skip that. Outputs

- `results/calibrated_comparison.md`  the comparison table (test and validation)
- `results/calibrated_comparison.json` the same numbers, machine readable
- `model/bp_calibrated.joblib`        the best model on validation + calibration settings

Options `--models "Random forest,Extra trees"` to run fewer models,
`--low-bp-weight 1` to switch off low-BP weighting, `--train-cals 10` for more
training pairs, `--use-cal-bp` to let the model see the cuff value (see below),
`--jobs 4` to limit CPU cores.

### What the model does

1. A cuff reading is taken; the 30 s PPG window at the same moment is the calibration window.
2. Every new window: the model predicts the change in SBP/DBP since calibration
   from the new pulse shape and how it differs from the calibration pulse.
3. Estimate = cuff value + predicted change.

### Rows in the table

| Row | Meaning |
|---|---|
| Baseline: hold last cuff value | predict no change. Every model must beat this |
| Old model (RF, no calibration) | the current siren_app model |
| New features, no calibration | new 25 Hz features, but absolute BP like before |
| Calibrated: ... | the new approach, five ML models |
| Reference: Extra trees + cuff value as input | also sees the cuff number; reported, never saved (pulls low BP towards normal) |

Columns: MAE, ME, SD (ISO 81060-2 needs |ME| ≤ 5 and SD ≤ 8), share within
10 mmHg, r (change) = correlation of predicted and true change, NEWS2 band
agreement, SBP ≤ 100 detection, 20 mmHg drop detection and its false alarm
rate, and direction agreement for true changes of 10 mmHg or more.

### Calibration settings (model/bp_calibrated.joblib, `config`)

| Setting | Default | Why |
|---|---|---|
| window_s | 30 | one PPG window per cuff reading |
| min_sqi | 0.90 | beat-template correlation needed to accept a calibration window |
| min_beats | 15 | beats needed in that window |
| max_motion_g | 0.05 | no calibration while the ambulance or patient is moving |
| max_interval_unstable_min | 10 | NEWS2 ≥ 5 or any red score (Sun et al.: error grew 5.4 to 9.4 mmHg within 10 min) |
| max_interval_stable_min | 15 | stable patients, matches usual EMS reassessment |
| stale_after_min | 20 | estimate hidden after this long without a cuff |
| trend_alert_mmhg | 20 | predicted change that triggers a cuff check |
| recal_on_band_change | on | predicted SBP crosses a NEWS2 SBP band |
| recal_on_finger_lost | on | sensor moved, old baseline invalid |
| hr_change_frac | 0.25 | HR changed 25% since calibration: cuff check |
| min_estimate_sqi | 0.80 | poor windows give no estimate |

These are starting values from the literature, to be tuned on SIREN volunteer data.

## Step 6, calibrated model on your own recording

```
python scripts/run_calibrated_on_log.py my_log.txt --cuff 118/76 --check 135/85@300
python scripts/run_calibrated_on_log.py my_log.txt --cuff 118/76 --check 130/84@300,125/80@600 --use-checks
```

`--use-checks` also feeds each later cuff reading to the model (multi-point
calibration); each check is compared with the estimate just before it.

Calibrates on the first 30 s with `--cuff`, then estimates every 5 s.
`--check` compares with later cuff readings (SBP/DBP@seconds). Do one rest
recording and one with a BP change (after exercise, or a cold pressor test)
with cuff readings every 2 to 3 min.

## Step 14, live BP on your own PPG (final settings)

```
pip install pyserial
python scripts/live_bp.py --port COM13
```

Needs the siren-sensors `ppg` firmware with `WAVEFORM_OUTPUT 1` (raw IR as
`W,<ms>,<fs>,<25 samples>` lines, `G,<ms>,<led>` when the automatic gain
changes the LED). Close the PlatformIO monitor and the dashboard first.

Runs `model/bp_calibrated_vitaldb.joblib` with the final schedule
(`smart_kalman`: Kalman fusion, cuff every 10 min plus any 5 mmHg or NEWS2 band
change, NEWS2 score from estimate - 0.3 sigma, last estimate held while a cuff
is pending) and prints an estimate every 5 s.

| Type | Meaning |
|---|---|
| `s` | the cuff is starting now; keeps the 30 s PPG window from before inflation |
| `118/76` | the cuff reading; calibrates with the window kept by `s` (without `s`, the window that ended `--entry-lag` 45 s earlier) and prints the error of the estimate just before it |
| `c 118/76` | check only, no calibration |
| `q` | quit; prints mean error, SD, MAE and the share within 5 / 10 / 15 mmHg |

Windows across an LED gain step are skipped (`GAIN_CHANGE`). A board restart
clears the calibration. Everything is saved in `results/live/`:
`<time>_raw.txt` (replay with `--replay`, or with step 6 using `--model vitaldb`)
and `<time>_bp.csv`.

## Step 15, signal quality gate (rescue) check

```
python scripts/quality_gate_check.py            # choose on validation, check on test
python scripts/quality_gate_check.py --write    # switch it on for the live model if it holds
```

The strict gate drops a whole 30 s window when a few beats are spoilt. The
rescue gate (`bp_model/quality.py`) also keeps a window whose score is lower
when most of its beats are clean. The script replays the SIREN schedule on
VitalDB, picks the rescue thresholds on validation patients (most extra
estimates with dangerous miss, SBP SD and "within one band" within a small
margin), reports strict vs rescue on test, and with `--write` stores them as
`"quality"` in `alert_settings_vitaldb.json` only if they hold on test. Model
inputs never change; only which windows are used. Without the `"quality"`
block the live model keeps the strict gate.

`live_bp.py` also picks the cleanest window: for each estimate, of the windows
ending in the last 10 s (`--lookback`); for a cuff, of the windows from 30 s
before `s` until the reading is typed. After `s` the screen pauses until the
reading (or `x`).

The PPG firmware (siren-sensors, `GAIN_LOCK 1`) freezes the LED current once
the IR level has been in range for 3 s, so motion no longer makes the
automatic gain step and spoil 30 s of windows.

## Important about MIMIC-BP timing

The 30 segments of each MIMIC-BP patient are spread over the ICU stay and are
not consecutive, so calibration and test windows are often hours apart. That
is a harder test than an ambulance run with a cuff every 10 to 15 min, and it
also rewards "regression to the mean", which is why the cuff value is not an
input by default.

## Step 7, multi-point calibration and alert thresholds (new)

```
python scripts/eval_multipoint.py --dataset "D:/dataverse_files"
```

Run after step 5 (it uses `model/bp_calibrated.joblib` and the feature cache).
Takes 1 to 3 minutes. Outputs

- `results/multipoint_comparison.md` accuracy with 1 to 4 cuff readings, alert tables
- `model/alert_settings.json`         tuned thresholds, read automatically by `CalibratedBpModel`

### Multi-point calibration

Every cuff reading is kept (newest 3). Each one gives its own estimate
(cuff + predicted change) and SIREN shows their mean. From the second cuff a
patient gain is also fitted, shrunk towards 1 by `gain_lambda` (chosen on
validation). In code, call `calibrate()` for every cuff reading; `reset()`
when the finger is lost.

### Alerts

| Alert | Fires when | Tuned for |
|---|---|---|
| DROP_ALERT | predicted SBP fell by `drop_alert_mmhg` or more since the last cuff | most sensitive threshold with ≤ 20% false alarms on validation |
| HYPOTENSION_ALERT | estimated SBP ≤ `hypotension_alert_sbp` | same |
| RISE_ALERT | predicted rise ≥ `trend_alert_mmhg` (20) | fixed |

Change the operating point with `--max-false-alarm 10` (fewer alarms) or look at
the high-sensitivity rows (`--target-sens 80`) in the report. Every alert means
"take a cuff reading now", never "treat".

## Step 8, download VitalDB (new)

```
pip install -r requirements.txt          # adds the vitaldb package
python scripts/vitaldb_download.py --n-cases 50
```

VitalDB (Lee et al., Sci Data 2022, open access, CC BY 4.0): 6,388 surgical
cases with finger PPG (SNUADC/PLETH) and, in many, an arterial line, all on
one clock. Unlike MIMIC-BP the windows are consecutive, so changes over 2 to
15 minutes can be learned and tested.

The script picks adult general-anaesthesia cases with PPG + arterial line,
downloads PLETH, arterial SBP/DBP and real cuff (NIBP) readings, and stores
only features and labels per 30 s window in `vitaldb_cache/` (a few hundred
kB per case). Start with 50 cases to see the speed (roughly 10 to 40 MB of
download per case), then raise `--n-cases` (e.g. 300). Rerunning skips cases
already downloaded, and retries failed ones.

## Step 9, train and test on VitalDB (v2)

```
python scripts/vitaldb_train.py
python scripts/vitaldb_train.py --no-rf                  # faster, gradient boosting only
python scripts/vitaldb_train.py --cuff-source nibp       # real cuff readings instead of simulated
```

Outputs `results/vitaldb_comparison.md`, `model/bp_calibrated_vitaldb.joblib`
and `model/alert_settings_vitaldb.json`. Report sections

1. What each improvement adds, all scored on the same clean test pairs:
   A v1 model, B clean windows only in training, C + recent trend (features
   now minus the last 30-120 s) and minutes since the cuff, D + output
   smoothing (window chosen on validation), E + ensemble of gradient
   boosting and random forest (kept only if it wins on validation).
2. Error against minutes since the cuff (evidence for the recalibration interval).
3. When to take the second cuff: none, 2, 5 or 10 min, with cuffs averaged
   equally or time-weighted (newer cuffs count more; half-life chosen on
   validation). Cuff error simulated (SD 0 and 6 mmHg) or real NIBP.
4. Drop and hypotension alerts with thresholds tuned on validation.
5. Cuff schedules and NEWS2: fixed cuffs every 15/10/5/3 min against a 10 min
   schedule with smart extra cuffs (estimate moves >= `--smart-trigger` mmHg,
   default 8, or changes NEWS2 SBP band; a drop-only variant too). Reports
   cuffs per 15 min, SBP MAE, BHS grade, NEWS2 band agreement and dangerous
   misses (true score >= 2 shown as 0), then a NEWS2 usability check (at least
   95% within one band, at most 5% dangerous misses, hypotension alert
   sensitivity at least 80%; change with `--news2-*`) and over-triage.
6. Kalman fusion (`bp_model/kalman.py`): fuses cuffs with the PPG estimate and
   gives an uncertainty (sigma) that grows with time since the cuff. Tested as
   (a) fused estimate, (b) cautious NEWS2 score from estimate - z x sigma, z
   chosen on validation, and (c) an extra cuff when the cautious score is worse
   than the last cuff's. Skip with `--no-kalman`. The tuned filter settings and
   z are saved in `alert_settings_vitaldb.json` (`kalman`, `news2_score_z`).

Band-balanced weights (`--band-weights inverse`, default; `none` to skip):
a second gradient boosting model is trained with weights N / N_band on each
pair's true NEWS2 SBP band (Lim et al. 2025), so rare low-BP bands count more.
It becomes the final model only if, on validation with 5 min cuffs, it cuts
dangerous misses by at least 0.5 points without raising SBP MAE by more than
0.3 mmHg. Row C2 in section 1 and the extra section 5 rows compare the two.

Pulse arrival time test (does an ECG lead help?):
```
python scripts/vitaldb_pat_download.py --n-cases 150   # ECG II + PLETH at 250 Hz, a few GB, 1-2 h, resumable
python scripts/vitaldb_pat_train.py                    # PPG only vs PPG + PAT, same cases and pairs
```
`bp_model/pat.py` finds R peaks (simplified Pan-Tompkins) and, per beat, the
time to the PPG foot, steepest upstroke and peak; 6 window features. The
download reuses case ids from `vitaldb_cache/` and keeps the same patient split.
`results/vitaldb_pat_comparison.md` compares single-cuff accuracy, how PAT
change correlates with SBP change (expected negative), and the NEWS2 check for
fixed 5 min, fixed 10 min and 10 min + any change with 95% CIs. Decide on the
ECG hardware from section 3: if PPG + PAT passes where PPG only fails, it is
worth building.

Breathing variation test (pleth variability, a sign of low blood volume):
```
python scripts/vitaldb_resp_download.py --n-cases 300   # PLETH only at 100 Hz, resumable
python scripts/vitaldb_pat_train.py --cache vitaldb_resp_cache --extra-label "PPG + breathing variation" --report vitaldb_resp_comparison.md
```
`bp_model/respvar.py` gives 5 gain-free features per 30 s window: pulse
height swing, its share in the breathing band, baseline swing, beat-interval
swing and pulse-width swing. Expected: swing grows when SBP falls.

Confirmatory cuff (off by default; set `confirm_mmhg` > 0, e.g. 10): when a cuff differs from the estimate just before it by more
than `confirm_mmhg`, one more cuff is asked for `confirm_after_min` (2)
later (reason CONFIRM); the confirmation never triggers another. In the
simulation `schedule_sim(..., confirm_mmhg=10)`; robustness_check.py has rows
for fixed 5 min and 10 min + any change with confirmation (`--confirm-mmhg`).
728-case result: 5 min 5.4 -> 5.0%, 10 min + 8 mmHg 5.1 -> 4.9%, 10 min + 5 mmHg
4.0 -> 4.2% for 0.2-0.35 extra cuffs per 15 min, so it stays off.

Cuff delay and cuff accuracy (final schedule): `python scripts/robustness_check.py --final`
tests the chosen SIREN schedule for every combination of `--delays` (minutes
from "cuff now" to the reading: crew response plus measurement time, default
0, 0.75, 1.5, 3) and `--noises` (cuff error SD, default 6 and 8), next to fixed
10 min cuffs at the same delay. An automatic monitor started by the
optocoupler is about 0.5-1 min; a crew pressing start, 1-3 min.
`schedule_sim(..., cuff_delay_min=...)`; `--cuff-delay` applies one delay to
the full table.

Robustness check (after vitaldb_train.py, about a minute):
`python scripts/robustness_check.py` repeats the NEWS2 schedule test with 5
random cuff errors and gives 95% confidence intervals by resampling test
patients. Verdict ROBUST (whole interval meets every criterion), BORDERLINE
(point value passes, interval crosses a limit) or NOT USABLE. Writes
`results/robustness_check.md`.

Cuff schedule (live): `CalibratedBpModel(..., schedule="smart_kalman")` is the
default: Kalman fusion of cuffs and PPG, a cuff every 10 min (5 min when
NEWS2 >= 5), plus an extra cuff whenever the fused estimate moves 5 mmHg from
the last cuff or changes NEWS2 SBP band (at least 2 min apart). The NEWS2 SBP score shown (`news2_sbp_score`) is taken from
estimate - 0.3 x sigma. Robustness check on 728 VitalDB cases (106 test
patients, 5 cuff-error repeats): dangerous miss 3.3% (95% CI 2.3-4.8), within
one band 96.3%, hypotension alert 90.4%, 4.2 cuffs per 15 min: ROBUST, the only
schedule whose whole interval met all three criteria. z 0.3 was chosen on
validation; the 5 mmHg trigger also passes on validation (miss 3.9%, within
one band 95.4%). In VitalDB the extra cuffs average about one every 3.6 min
because anaesthesia BP moves a lot; a stable patient gets far fewer.
`schedule="smart"` = same triggers without Kalman (4.0%, BORDERLINE);
`schedule="kalman"` = 3 min + uncertainty cuffs (4.4%, BORDERLINE). Overrides in
the settings file: `"schedule"`, `"smart_trigger_mmhg"`, `"news2_score_z_smart"`.

While waiting for a requested cuff (live): `cuff_now` stays true until
`calibrate()` arrives and `status` is `AWAITING_CUFF`. With the default
`pending_display="hold_estimate"` the dashboard keeps showing the estimate and
NEWS2 SBP score from the moment the cuff was asked for (`held` true); the live
estimate is still returned as `live_sbp` / `live_dbp` and drop, hypotension,
rise and HR alerts keep using it. After `cuff_overdue_min` (5) without a cuff,
reason `CUFF_OVERDUE`. `"live"` keeps updating, `"hold_cuff"` shows the last
cuff. Test the cost with `robustness_check.py --final --displays live,hold_estimate,hold_cuff`.

Kalman mode (live, `schedule="kalman"`): when the settings file has `kalman` (written by
vitaldb_train.py section 6), `CalibratedBpModel` fuses every cuff with the PPG
estimate and returns `sbp_sigma`, `sbp_range_95` and `news2_sbp_score`, the
cautious score from estimate - z x sigma that the dashboard should use for
NEWS2. A cuff is requested every 3 min (`max_interval_kalman_min`) or earlier
(reason UNCERTAINTY, at least 2 min after the last cuff) when the cautious score
is worse than the last cuff's. 3 min is the only interval that met the NEWS2
check after the robustness check (5 min with uncertainty cuffs: 5.4% dangerous
misses over 5 repeats, above the 5% limit). Drop,
hypotension, rise and HR alerts are still reported. `mode` says which logic
ran; set `use_kalman=False` in the config for the old smart triggers.

Live use: `CalibratedBpModel.estimate()` returns `cuff_now` (take a cuff now)
and `next_cuff_due_min`, with reasons such as INTERVAL, SMART_CHANGE,
NEWS2_BAND_CHANGE, DROP_ALERT, HYPOTENSION_ALERT. Defaults: cuff every 5 min
(NEWS2 >= 5) or 10 min (stable), extra cuffs no closer than 2 min apart. A model
saved before 22 Nov keeps its old interval settings until retrained.

The saved model carries its settings (layout, smoothing, half-life), and
`CalibratedBpModel` applies them automatically. SIREN only estimates on clean
windows (SQI >= 0.80), so the test is scored on clean windows too.

Use the VitalDB model on your own log with
`python scripts/run_calibrated_on_log.py my_log.txt --cuff 118/76 --model vitaldb`.

VitalDB patients are anaesthetised (drug-driven BP changes, not trauma) and
the PPG is a bedside oximeter, so SIREN volunteer data is still needed.
