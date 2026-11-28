"""Quick automatic checks. Run with:  python -m pytest -q"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bp_model import (FEATURE_NAMES, BpModel, extract_features, news2_sbp_score,  # noqa: E402
                      parse_waveform_log, prepare_window)
from bp_model.synthetic import max30102_ir  # noqa: E402


@pytest.fixture(scope="module")
def model():
    return BpModel()


def test_model_loads(model):
    assert type(model.model).__name__ == "RandomForestRegressor"
    assert model.feature_version == "signal_summary_v1"
    assert model.model.n_features_in_ == 36 == len(FEATURE_NAMES)


def test_feature_vector_shape():
    _, sig = max30102_ir(32)
    assert extract_features(sig).shape == (36,)


def test_prepare_window_statuses(model):
    t, ir = max30102_ir(10)
    assert prepare_window(t, ir, model.reference)[1] == "COLLECTING"
    t, ir = max30102_ir(32)
    assert prepare_window(t, np.full_like(ir, 1000.0), model.reference)[1] == "POOR_SIGNAL"
    w, status, _ = prepare_window(t, ir, model.reference)
    assert status == "OK" and w.shape == (3750,)
    gap = t.copy(); gap[400:] += 2000  # 2 s hole
    assert prepare_window(gap, ir, model.reference)[1] == "COLLECTING"


def test_prediction_plausible_and_deterministic(model):
    t, ir = max30102_ir(32)
    a = model.predict_from_sensor(t, ir)
    assert a == model.predict_from_sensor(t, ir)
    sbp, dbp, status = a
    assert status == "OK"
    assert 70 < sbp < 200 and 30 < dbp < 130 and sbp > dbp


def test_gain_invariance(model):
    t, ir = max30102_ir(32)
    _, ir2 = max30102_ir(32, dc=240000)
    assert abs(model.predict_from_sensor(t, ir)[0] - model.predict_from_sensor(t, ir2)[0]) < 0.5


def test_parse_sample_log():
    t, ir = parse_waveform_log(ROOT / "sample_data" / "sample_serial_log.txt")
    assert t.size > 1500
    assert np.all(np.diff(t) > 0)
    assert abs(1000 / np.median(np.diff(t)) - 25) < 0.5


@pytest.mark.parametrize("sbp,score", [(85, 3), (90, 3), (95, 2), (100, 2), (105, 1), (110, 1), (111, 0), (219, 0), (220, 3)])
def test_news2_bands(sbp, score):
    assert news2_sbp_score(sbp) == score


# ---------------------------------------------------------------- new model
from bp_model.features_v2 import N_FEATURES, extract_sensor  # noqa: E402


def test_v2_features_from_sensor():
    t, ir = max30102_ir(30)
    f = extract_sensor(ir)
    assert f.shape == (N_FEATURES,)
    assert np.isfinite(f).sum() >= N_FEATURES - 2
    assert 60 < f[-6] < 90            # hr_bpm, synthetic HR is 75
    assert f[-2] > 0.9                # template correlation on a clean signal


def test_v2_features_gain_invariant():
    _, ir1 = max30102_ir(30)
    _, ir2 = max30102_ir(30, dc=240000)          # LED gain doubled
    a, b = extract_sensor(ir1), extract_sensor(ir2)
    # Only integer rounding of the counts differs, so allow a few percent
    assert np.allclose(a, b, rtol=0.05, atol=0.05, equal_nan=True)


def test_v2_flat_signal_gives_nan():
    assert np.isnan(extract_sensor(np.full(750, 1000.0))).all()


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated.joblib").exists(),
                    reason="run scripts/retrain_calibrated.py first")
def test_calibrated_model_flow():
    from bp_model.calibrated import CalibratedBpModel
    m = CalibratedBpModel()
    t, ir = max30102_ir(32, seed=1)
    assert m.estimate(t, ir)["status"] == "NEEDS_CALIBRATION"
    ok, why = m.calibrate(t, ir, 120, 80)
    assert ok, why
    t2, ir2 = max30102_ir(32, seed=2)
    r = m.estimate(t2 + 60000, ir2)
    assert r["status"] == "OK" and abs(r["delta_sbp"]) < 15
    r = m.estimate(t2 + 16 * 60000, ir2)
    assert "INTERVAL" in r["recalibrate_reasons"]


def test_multipoint_gain():
    from bp_model.multipoint import combine, fit_gain, pair_arrays
    assert fit_gain(np.array([]), np.array([]), 100) == 1.0
    d = np.array([10.0, -10.0]); y = np.array([20.0, -20.0])
    assert fit_gain(d, y, 0) == pytest.approx(2.0)
    assert 1.0 < fit_gain(d, y, 100) < 2.0                    # shrunk towards 1
    vals = np.array([120.0, 100.0])
    P = np.array([[0.0, -10.0], [10.0, 0.0]])
    dd, yy = pair_arrays(vals, P)
    assert set(zip(dd, yy)) == {(-10.0, -20.0), (10.0, 20.0)}
    est = combine(vals, np.array([[0.0], [0.0]]), 1.0)
    assert est[0] == pytest.approx(110.0)                       # mean of the two anchors


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated.joblib").exists(),
                    reason="run scripts/retrain_calibrated.py first")
def test_calibrated_two_cuffs():
    from bp_model.calibrated import CalibratedBpModel
    m = CalibratedBpModel()
    t, ir = max30102_ir(32, seed=1)
    assert m.calibrate(t, ir, 120, 80)[0]
    t2, ir2 = max30102_ir(32, seed=3, hr_bpm=90)
    assert m.calibrate(t2 + 600000, ir2, 105, 70)[0]
    r = m.estimate(t2 + 660000, ir2)
    assert r["cuffs_used"] == 2 and 95 < r["sbp"] < 130
    m.reset()
    assert m.estimate(t2, ir2)["status"] == "NEEDS_CALIBRATION"


def test_vitaldb_processing():
    sys.path.insert(0, str(ROOT / "scripts"))
    import vitaldb_download as vd
    from bp_model.synthetic import upright_ppg
    fs = 100.0
    _, pleth = upright_ppg(300, fs, hr_bpm=70)            # 5 minutes
    n = pleth.size
    sbp = np.full(n, np.nan); dbp = np.full(n, np.nan)
    k = np.arange(0, n, 200)
    sbp[k], dbp[k] = 120.0, 70.0
    sbp[k[30:33]] = 300.0                                 # flush artefact in window 2
    nibp_s = np.full(n, np.nan); nibp_d = np.full(n, np.nan)
    nibp_s[6000:6400:200], nibp_d[6000:6400:200] = 115.0, 72.0   # one cuff, repeated by the monitor
    r = vd.process_case(np.column_stack([pleth, sbp, dbp, nibp_s, nibp_d]), fs)
    assert len(r["t"]) == 9                               # 10 windows, 1 rejected
    assert np.allclose(r["L"], [120.0, 70.0])
    assert r["F"].shape == (9, 26)
    assert r["nibp"].shape == (1, 3) and r["nibp"][0, 1] == 115.0


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated_vitaldb.joblib").exists(),
                    reason="run scripts/vitaldb_train.py first")
def test_vitaldb_model_flow():
    from bp_model.calibrated import MODEL_DIR, CalibratedBpModel
    m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", MODEL_DIR / "alert_settings_vitaldb.json")
    t, ir = max30102_ir(32, seed=1)
    assert m.calibrate(t, ir, 120, 80)[0]
    for k in range(1, 4):                       # a few estimates fill the trend and smoothing history
        t2, ir2 = max30102_ir(32, seed=k + 1)
        r = m.estimate(t2 + k * 60000, ir2)
    assert r["status"] == "OK" and 95 < r["sbp"] < 145
    assert m.calibrate(t2 + 5 * 60000, ir2, 110, 75)[0]
    assert m.estimate(t2 + 6 * 60000, ir2)["cuffs_used"] == 2


def test_schedule_sim_runs():
    sys.path.insert(0, str(ROOT / "scripts"))
    import vitaldb_train as vt
    T = np.array([[95.0, 60.0], [120.0, 70.0], [130.0, 80.0]])
    m = vt.schedule_metrics(T, T + np.array([[3.0, 1.0], [12.0, 2.0], [-4.0, 0.0]]))
    assert m["BHS"] in "ABCD" and m["band_within1"] == 100.0
    assert list(vt.news2_band([85, 95, 105, 150, 230])) == [3, 2, 1, 0, 3]


def test_kalman_filter():
    from bp_model.kalman import BpKalman, KalmanConfig, fit_error_growth
    kf = BpKalman(KalmanConfig(q=4.0, cuff_sd=6.0, s0=5.0, s1=1.0))
    x, s = kf.cuff(0.0, [120.0, 70.0])
    assert np.allclose(x, [120, 70]) and abs(s[0] - 6.0) < 1e-9
    sig = []
    for k in range(1, 21):                       # PPG says BP is falling
        x, s = kf.ppg(30.0 * k, [120.0 - k, 70.0], 0.5 * k)
        sig.append(s[0])
    assert x[0] < 115 and sig[-1] > sig[3]       # follows the fall, less sure as the cuff ages
    x2, s2 = kf.cuff(630.0, [100.0, 65.0])
    assert s2[0] < sig[-1] and x2[0] < x[0]      # a cuff pulls it back and cuts the uncertainty
    s0, s1 = fit_error_growth(np.repeat(np.arange(15.0), 100), np.tile(np.linspace(-1, 1, 100), 15)
                              * (5 + np.repeat(np.arange(15.0), 100)))
    assert s0 > 1 and s1 > 0


def test_schedule_kalman_options():
    sys.path.insert(0, str(ROOT / "scripts"))
    import vitaldb_train as vt
    from bp_model.kalman import KalmanConfig

    class Flat:                                   # predicts no change
        def predict(self, X):
            return np.zeros((len(X), 2))
    rng = np.random.default_rng(0)
    n = 80
    t = np.arange(n) * 30.0 + 30
    L = np.column_stack([np.linspace(130, 92, n), np.linspace(75, 55, n)])
    F = rng.normal(size=(n, 26)); F[:, -2] = 1.0
    c = {"t": t, "F": F, "L": L, "sqi": np.ones(n), "Frec": F.copy()}
    T, E, H, nc, x = vt.schedule_sim([c], Flat(), 10, None, 0.0, rng, kalman=KalmanConfig(), score_z=1.0,
                                     unc_trigger=True, extra=True)
    assert len(T) and np.isfinite(x["sigma"]).all() and len(x["band"]) == len(T)
    m = vt.schedule_metrics(T, E, 100.0, {"within1": 95, "max_miss": 5, "min_hypo_sens": 80}, PB=x["band"])
    assert "overcall" in m and "news2_usable" in m


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated_vitaldb.joblib").exists(),
                    reason="run scripts/vitaldb_train.py first")
def test_live_kalman_mode(tmp_path):
    import json
    from bp_model.calibrated import MODEL_DIR, CalibratedBpModel
    from bp_model.kalman import KalmanConfig
    st = {"hypotension_alert_sbp": 106.5, "drop_alert_mmhg": 5.6, "max_anchors": 3,
          "kalman": KalmanConfig(q=32.0, s0=9.0, s1=0.6).to_dict(), "news2_score_z": 0.5}
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(st))
    m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", p, schedule="kalman")
    t, ir = max30102_ir(32, seed=1)
    assert m.calibrate(t, ir, 112, 70)[0]
    r = m.estimate(t + 60000, ir)
    assert r["mode"] == "kalman" and r["sbp_sigma"] > 0
    lo, hi = r["sbp_range_95"]
    assert lo < r["sbp"] < hi
    # near the 110 edge the cautious score can be worse than the plain band, never better
    from bp_model.calibrated import news2_sbp_score
    assert r["news2_sbp_score"] >= news2_sbp_score(r["sbp"])
    r = m.estimate(t + 6 * 60000, ir)
    assert r["cuff_now"] and "INTERVAL" in r["recalibrate_reasons"]      # 3 min interval in Kalman mode


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated_vitaldb.joblib").exists(),
                    reason="run scripts/vitaldb_train.py first")
def test_live_smart_schedule_default():
    from bp_model.calibrated import MODEL_DIR, CalibratedBpModel
    import json
    st = json.loads((MODEL_DIR / "alert_settings_vitaldb.json").read_text())
    m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", MODEL_DIR / "alert_settings_vitaldb.json")
    want = "smart_kalman" if "kalman" in st else "smart"
    assert m.schedule == want and m.trigger_mmhg == 5.0
    assert (m.kf is not None) == (want == "smart_kalman") and (want == "smart" or m.score_z == 0.3)
    t, ir = max30102_ir(32, seed=1)
    assert m.calibrate(t, ir, 120, 75)[0]
    r = m.estimate(t + 60000, ir)
    assert r["mode"] == want and r["next_cuff_due_min"] == 9.0       # 10 min base for a stable patient
    r = m.estimate(t + 11 * 60000, ir)
    assert r["cuff_now"] and "INTERVAL" in r["recalibrate_reasons"]


def test_pat_features_track_delay():
    from bp_model.pat import pat_features, r_peaks, synthetic_ecg_ppg
    vals = []
    for pat in (0.20, 0.25, 0.30):
        e, p, beats = synthetic_ecg_ppg(pat_s=pat, seed=1)
        assert abs(len(r_peaks(e, 250.0)) - len(beats)) <= 1
        vals.append(pat_features(e, p, 250.0))
    slope = [v[1] for v in vals]
    assert abs((slope[1] - slope[0]) - 50) < 8 and abs((slope[2] - slope[1]) - 50) < 8   # 50 ms steps recovered
    assert abs(vals[0][5] - 72) < 2                                                       # ECG heart rate
    assert np.isnan(pat_features(np.zeros(7500), np.zeros(7500), 250.0)).all()


def test_pat_download_processing():
    sys.path.insert(0, str(ROOT / "scripts"))
    import vitaldb_pat_download as d
    from bp_model.pat import synthetic_ecg_ppg
    segs = [synthetic_ecg_ppg(30.0, 250.0, 70, 0.25 + 0.01 * k, seed=k)[:2] for k in range(4)]
    wave = np.column_stack([np.concatenate([s[0] for s in segs]), np.concatenate([s[1] for s in segs])])
    num = np.full((120, 4), np.nan)
    num[:, 0], num[:, 1] = 120.0, 70.0
    num[40, 2:] = [115, 70]
    r = d.process(wave, num)
    assert r["P"].shape == (4, 6) and np.all(np.diff(r["P"][:, 1]) > 0)
    assert r["nibp"].shape == (1, 3)


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated_vitaldb.joblib").exists(),
                    reason="run scripts/vitaldb_train.py first")
def test_live_confirmatory_cuff():
    from bp_model.calibrated import MODEL_DIR, CalibratedBpModel
    m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", MODEL_DIR / "alert_settings_vitaldb.json")
    assert m.config.confirm_mmhg == 0                              # off by default
    m.config.confirm_mmhg = 10
    t, ir = max30102_ir(32, seed=1)
    assert m.calibrate(t, ir, 120, 75)[0]
    r = m.estimate(t + 60000, ir)
    est = r["sbp"]
    assert m.calibrate(t + 120000, ir, est - 25, 60)[0]          # cuff far from the estimate
    r = m.estimate(t + 180000, ir)
    assert "CONFIRM" not in r["recalibrate_reasons"]
    r = m.estimate(t + 250000, ir)                                # 2 min after that cuff
    assert "CONFIRM" in r["recalibrate_reasons"] and r["cuff_now"]
    assert m.calibrate(t + 260000, ir, est - 24, 60)[0]           # the confirmation itself
    r = m.estimate(t + 400000, ir)
    assert "CONFIRM" not in r["recalibrate_reasons"]


def test_resp_variation_features():
    from bp_model.respvar import resp_features, synthetic_ppg
    a = resp_features(synthetic_ppg(resp_depth=0.02, seed=1), 100.0)
    b = resp_features(synthetic_ppg(resp_depth=0.15, seed=1), 100.0)
    assert b[0] > 3 * a[0] and b[1] > 0.8                 # bigger swing, in the breathing band
    assert np.isnan(resp_features(np.zeros(3000), 100.0)).all()


def test_resp_download_processing():
    sys.path.insert(0, str(ROOT / "scripts"))
    import vitaldb_resp_download as d
    from bp_model.respvar import synthetic_ppg
    ppg = np.concatenate([synthetic_ppg(30.0, 100.0, 75, 12, 0.05 * k, seed=k) for k in range(4)])
    num = np.full((120, 4), np.nan)
    num[:, 0], num[:, 1] = 120.0, 70.0
    r = d.process(ppg, num)
    assert r["P"].shape == (4, 5) and np.all(np.diff(r["P"][1:, 0]) > 0)


@pytest.mark.skipif(not (ROOT / "model" / "bp_calibrated_vitaldb.joblib").exists(),
                    reason="run scripts/vitaldb_train.py first")
def test_live_hold_while_awaiting_cuff():
    from bp_model.calibrated import MODEL_DIR, CalibratedBpModel
    m = CalibratedBpModel(MODEL_DIR / "bp_calibrated_vitaldb.joblib", MODEL_DIR / "alert_settings_vitaldb.json")
    assert m.config.pending_display == "hold_estimate"
    t, ir = max30102_ir(32, seed=1)
    _, ir2 = max30102_ir(32, seed=5)
    assert m.calibrate(t, ir, 120, 75)[0]
    r = m.estimate(t + 11 * 60000, ir)                   # 10 min interval passed: request opens
    assert r["cuff_now"] and r["status"] in ("OK", "AWAITING_CUFF") and not r["held"]
    held = r["sbp"]
    r = m.estimate(t + 12 * 60000, ir2)                  # still waiting: value held, live value still computed
    assert r["held"] and r["sbp"] == held and r["cuff_now"] and r["status"] == "AWAITING_CUFF"
    assert "live_sbp" in r and r["awaiting_cuff_min"] == 1.0
    r = m.estimate(t + 17 * 60000, ir2)
    assert "CUFF_OVERDUE" in r["recalibrate_reasons"]
    assert m.calibrate(t + 17 * 60000 + 30000, ir2, 118, 74)[0]   # cuff arrives: request closed
    r = m.estimate(t + 18 * 60000, ir2)
    assert not r["held"] and r["status"] == "OK"


def test_live_stream_parses_firmware_lines_and_skips_gain_steps():
    import io
    sys.path.insert(0, str(ROOT / "scripts"))
    import live_bp as lb
    t, ir = max30102_ir(100, seed=2)
    lines = ["SIREN PPG node"]
    for b in range(0, t.size - 24, 25):
        lines.append(f"W,{int(t[b + 24])},25.00," + ",".join(map(str, ir[b:b + 25].astype(int))))
        if b == 25 * 50:
            lines.append(f"G,{int(t[b + 24])},70")
    s = lb.Stream()
    lb.reader(iter(lines), s, io.StringIO(), 0.0)
    assert s.ended and abs(len(s.t) - t.size) < 25
    g = s.gain_ms[0]
    assert s.window(g + 10000)[1] == "GAIN_CHANGE"     # window spans the LED step
    win, why = s.window(g - 1000)                      # window before it is usable
    assert why == "OK" and win[0].size >= 25 * 29 and win[0][-1] <= g - 1000
    assert s.window(g + 32000)[1] == "OK"              # 31 s later the step is out of the window


def test_quality_rescue_gate_keeps_mostly_clean_windows_only():
    from bp_model.quality import QualityGate, clean_frac, effective_sqi, ok_calibrate, ok_estimate
    F = np.full((3, 26), np.nan)
    F[:, -6] = 72.0                       # 36 beats expected in 30 s
    F[:, -3] = [34, 30, 12]               # clean beats kept
    F[:, -2] = [0.95, 0.62, 0.62]         # score over all beats
    strict, rescue = QualityGate(), QualityGate(rescue=True)
    assert list(ok_estimate(F, strict)) == [True, False, False]
    assert list(ok_estimate(F, rescue)) == [True, True, False]       # 83% clean beats rescued, 33% not
    assert list(ok_calibrate(F, rescue)) == [True, False, False]     # calibration stays strict here (0.62 < 0.75)
    assert abs(clean_frac(F)[1] - 30 / 36) < 1e-9
    q = effective_sqi(F, rescue)
    assert q[0] == 0.95 and q[1] == 0.80 and q[2] == 0.62
