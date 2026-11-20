#include "respiration.h"
#include <math.h>

// Scratch buffers at file scope so nothing large lands on the loop task stack
static float w_lin[RESP_WINDOW];
static float w_hp[RESP_WINDOW];
static float w_lp[RESP_WINDOW];

// ------------------------------------------------------------- lifecycle ----
void RespirationMonitor::begin() {
    reset();
}

void RespirationMonitor::reset() {
    _head = 0;
    _filled = 0;
    _beatGrid = 0;
    _gridStartMs = 0;
    _accum = 0;
    _accumN = 0;
    _badNow = false;
    _spliceNext = false;
    _riivOffset = 0.0f;
    _riivLast = NAN;

    _lastMs = 0;
    _fsEst = 25.0f;
    _baseline = NAN;
    _env = 0.0f;
    _holdoffPending = false;
    _holdoffUntil = 0;

    _acPrev1 = _acPrev2 = 0.0f;
    _tPrev1 = _tPrev2 = 0;

    _lastBeatTime = 0.0f;
    _meanInterval = 0.0f;
    _beatAmp = NAN;
    _beatInterval = NAN;
    _beatCount = 0;

    _beatHead = 0;
    _beatFilled = 0;
    _warmup = 0;

    _histN = 0;
    _histHead = 0;
}

void RespirationMonitor::markDiscontinuity() {
    // Drop the partial grid sample, which straddles the step, and splice the
    // next one onto the previous level.
    _accum = 0;
    _accumN = 0;
    _spliceNext = true;

    // Re-acquire the beat detector baseline at the new level. Without this the
    // step itself looks like an enormous pulse.
    _baseline = NAN;
    _env = 0.0f;
    _acPrev1 = _acPrev2 = 0.0f;
    _tPrev1 = _tPrev2 = 0;
    _holdoffPending = true;
}

void RespirationMonitor::setArtefact(bool on, uint32_t lookbackMs) {
    if (on && lookbackMs > 0 && _filled > 0) {
        int k = (int)ceilf(lookbackMs / 1000.0f * RESP_FS);
        if (k > _filled) k = _filled;
        for (int i = 1; i <= k; i++) {
            _bad[(_head - i + RESP_WINDOW) % RESP_WINDOW] = 1;
        }
    }
    _badNow = on;

    if (on) {
        // Motion inflates the pulse envelope many times over, so the beat
        // threshold stays too high for seconds after the motion ends, and the
        // last beat values (taken during motion) would be held into the clean
        // signal that follows. Restart beat tracking; the beat series resume
        // from the first good beat after the artefact.
        _env = 0.0f;
        _beatAmp = NAN;
        _beatInterval = NAN;
        _beatGrid = 0;
        _lastBeatTime = 0.0f;
        _meanInterval = 0.0f;
        _beatFilled = 0;
        _beatHead = 0;
        _warmup = 3;
    }
}

// Number of consecutive clean grid samples ending at the newest one
int RespirationMonitor::cleanSamples() const {
    int n = 0;
    while (n < _filled && !_bad[(_head - 1 - n + RESP_WINDOW) % RESP_WINDOW]) n++;
    return n;
}

// ---------------------------------------------------------- sample intake ---
void RespirationMonitor::addSample(uint32_t ir, uint32_t nowMs) {
    float value = (float)ir;

    float dt = (_lastMs == 0) ? 0.04f : (nowMs - _lastMs) / 1000.0f;
    if (dt <= 0.0f || dt > 1.0f) dt = 0.04f;
    _lastMs = nowMs;
    _fsEst += 0.05f * (1.0f / dt - _fsEst);

    if (_holdoffPending) {
        _holdoffUntil = nowMs + RESP_HOLDOFF_MS;
        _holdoffPending = false;
    }

    // DC level, used only to express pulse amplitude as AC/DC
    if (isnan(_baseline)) {
        _baseline = value;
        _hpIn1 = value; _hpOut1 = 0.0f;
        _hpIn2 = 0.0f;  _hpOut2 = 0.0f;
    }
    float alphaBase = dt / (2.0f + dt);
    _baseline += alphaBase * (value - _baseline);

    // Beat detection signal: two cascaded first-order high passes at 0.5 Hz.
    // Respiration moves the PPG baseline (that is RIIV), and with a single
    // slow baseline follower the low-amplitude beats on a falling baseline
    // dropped under the detection threshold. Two stages attenuate 0.25 Hz
    // breathing to about 20 % and keep a 75 bpm pulse at about 87 %.
    const float hpTau = 0.318f;                 // 1 / (2 pi 0.5 Hz)
    float aHp = hpTau / (hpTau + dt);
    _hpOut1 = aHp * (_hpOut1 + value - _hpIn1);
    _hpIn1  = value;
    _hpOut2 = aHp * (_hpOut2 + _hpOut1 - _hpIn2);
    _hpIn2  = _hpOut1;

    // In reflectance PPG the IR count dips at systole, so inverting the
    // high-passed signal turns those dips into positive peaks.
    float ac = -_hpOut2;

    float alphaEnv = dt / (3.0f + dt);
    if (ac > _env) _env = ac;
    else           _env -= alphaEnv * _env;

    // --- three-point peak test with parabolic refinement ---
    float threshold = 0.35f * _env;
    bool holdoff = (int32_t)(nowMs - _holdoffUntil) < 0;
    if (!holdoff && _env > 20.0f && _tPrev2 != 0) {
        bool isPeak = (_acPrev1 > _acPrev2) && (_acPrev1 >= ac) && (_acPrev1 > threshold);
        if (isPeak) {
            float refractory = 300.0f;
            if (_meanInterval > 0.0f && 0.5f * _meanInterval > refractory) {
                refractory = 0.5f * _meanInterval;
            }

            // Parabolic vertex through the peak and its neighbours: refined
            // time and refined height. At a low sample rate the raw sample
            // height depends on where the sample fell on the peak, and that
            // sampling phase cycles slowly from beat to beat, which looks
            // exactly like a respiratory amplitude modulation.
            float peakTime = (float)_tPrev1;
            float peakVal  = _acPrev1;
            float curve = _acPrev2 - 2.0f * _acPrev1 + ac;
            if (fabsf(curve) > 1e-6f) {
                float delta = 0.5f * (_acPrev2 - ac) / curve;
                if (delta > -1.0f && delta < 1.0f) {
                    float samplePeriod = ((float)nowMs - (float)_tPrev2) * 0.5f;
                    peakTime += delta * samplePeriod;
                    peakVal  -= 0.25f * (_acPrev2 - ac) * delta;
                }
            }

            float beatTime = peakTime;

            if (_lastBeatTime <= 0.0f || (beatTime - _lastBeatTime) >= refractory) {
                // Amplitude relative to the baseline (AC/DC, per mille), so an
                // LED current change does not show up as an amplitude step.
                float rel = (_baseline > 1.0f) ? 1000.0f * peakVal / _baseline : NAN;
                registerBeat(beatTime, rel);
            }
        }
    }
    _acPrev2 = _acPrev1; _tPrev2 = _tPrev1;
    _acPrev1 = ac;       _tPrev1 = nowMs;

    // --- accumulate onto the uniform 4 Hz grid ---
    if (_gridStartMs == 0) _gridStartMs = nowMs;
    _accum += value;
    _accumN++;

    while ((int32_t)(nowMs - _gridStartMs) >= 250) {
        pushGrid();
        _gridStartMs += 250;
    }
}

void RespirationMonitor::registerBeat(float timeMs, float amplitude) {
    if (_lastBeatTime > 0.0f) {
        float iv = timeMs - _lastBeatTime;
        if (iv >= BEAT_IV_MIN && iv <= BEAT_IV_MAX) {
            // After a restart the first beats only re-establish the detection
            // threshold; their heights and intervals are not trusted yet.
            if (_warmup > 0) {
                _warmup--;
            } else {
                _beatInterval = iv;
                if (!isnan(amplitude)) _beatAmp = amplitude;
            }
            _beatCount++;
            _meanInterval = (_meanInterval <= 0.0f)
                          ? iv
                          : _meanInterval + 0.20f * (iv - _meanInterval);
        } else {
            _beatFilled = 0;
            _beatHead = 0;
            _meanInterval = 0.0f;
        }
    }

    _beatTimes[_beatHead] = timeMs;
    _beatHead = (_beatHead + 1) % BEAT_RING;
    if (_beatFilled < BEAT_RING) _beatFilled++;

    _lastBeatTime = timeMs;
}

void RespirationMonitor::pushGrid() {
    int prev = (_head - 1 + RESP_WINDOW) % RESP_WINDOW;

    // RIIV as log intensity. A change in LED current scales the DC level, which
    // in the log domain is a pure offset that can be spliced out.
    float riivValue;
    if (_accumN > 0 && _accum > 0) {
        float logv = logf((float)(_accum / _accumN));
        if (_spliceNext && !isnan(_riivLast)) {
            _riivOffset = logv - _riivLast;
        }
        _spliceNext = false;
        riivValue = 1000.0f * (logv - _riivOffset);   // per mille units
    } else {
        riivValue = isnan(_riivLast) ? 0.0f : _riivLast * 1000.0f;
    }
    _riivLast = riivValue / 1000.0f;
    _accum = 0;
    _accumN = 0;

    _riiv[_head] = riivValue;
    _bad[_head]  = _badNow ? 1 : 0;

    // The beat series only start once a beat exists. Zero-order hold between
    // beats; the low pass downstream removes the step edges.
    bool haveBeat = !isnan(_beatAmp) && !isnan(_beatInterval);
    if (haveBeat) {
        _riav[_head] = _beatAmp;
        _rifv[_head] = _beatInterval;
        if (_beatGrid < RESP_WINDOW) _beatGrid++;
    } else {
        _riav[_head] = 0.0f;
        _rifv[_head] = 0.0f;
    }
    (void)prev;

    _head = (_head + 1) % RESP_WINDOW;
    if (_filled < RESP_WINDOW) _filled++;
}

// -------------------------------------------------------------- heart rate --
BeatStats RespirationMonitor::beatRate() const {
    BeatStats s;
    if (_beatFilled < 5) return s;

    float t[BEAT_RING];
    uint8_t start = (uint8_t)((_beatHead - _beatFilled + BEAT_RING) % BEAT_RING);
    for (uint8_t i = 0; i < _beatFilled; i++) {
        t[i] = _beatTimes[(start + i) % BEAT_RING];
    }

    float iv[BEAT_RING];
    uint8_t n = 0;
    for (uint8_t i = 1; i < _beatFilled; i++) {
        float d = t[i] - t[i - 1];
        if (d >= BEAT_IV_MIN && d <= BEAT_IV_MAX) iv[n++] = d;
    }
    if (n < 4) return s;

    float sorted[BEAT_RING];
    for (uint8_t i = 0; i < n; i++) sorted[i] = iv[i];
    for (uint8_t i = 1; i < n; i++) {
        float key = sorted[i];
        int8_t j = i - 1;
        while (j >= 0 && sorted[j] > key) { sorted[j + 1] = sorted[j]; j--; }
        sorted[j + 1] = key;
    }
    float median = sorted[n / 2];
    if (median <= 0.0f) return s;

    float sum = 0.0f;
    uint8_t k = 0;
    for (uint8_t i = 0; i < n; i++) {
        if (fabsf(iv[i] - median) <= BEAT_TRIM * median) { sum += iv[i]; k++; }
    }
    if (k < 4) return s;
    float mean = sum / k;

    float var = 0.0f;
    for (uint8_t i = 0; i < n; i++) {
        if (fabsf(iv[i] - median) <= BEAT_TRIM * median) {
            var += (iv[i] - mean) * (iv[i] - mean);
        }
    }

    float bpm = 60000.0f / mean;
    if (bpm < 30.0f || bpm > 220.0f) return s;

    s.bpm = bpm;
    s.sdnn = sqrtf(var / k);
    s.intervals = k;
    s.valid = true;
    return s;
}

// ----------------------------------------------------------- estimation -----
// Copy the newest n grid samples into out, oldest first
void RespirationMonitor::tail(const float *ring, int n, float *out) const {
    int start = (_head - n + RESP_WINDOW) % RESP_WINDOW;
    for (int i = 0; i < n; i++) out[i] = ring[(start + i) % RESP_WINDOW];
}

float RespirationMonitor::estimate(const float *series, int n) const {
    if (n < RESP_MIN_FILL) return NAN;

    // Every accepted period must fit at least twice in the window
    int lagMax = RESP_LAG_MAX;
    if (lagMax > n / 2) lagMax = n / 2;
    if (lagMax <= RESP_LAG_MIN + 1) return NAN;

    // High pass at roughly 0.1 Hz by subtracting a 10 s moving average
    const int hpWidth = 40;
    for (int i = 0; i < n; i++) {
        int a = i - hpWidth / 2; if (a < 0) a = 0;
        int b = i + hpWidth / 2 + 1; if (b > n) b = n;
        float sum = 0.0f;
        for (int j = a; j < b; j++) sum += series[j];
        w_hp[i] = series[i] - (sum / (b - a));
    }

    // Low pass with a 3-point moving average (about 0.13 gain at 1.2 Hz)
    w_lp[0] = w_hp[0];
    w_lp[n - 1] = w_hp[n - 1];
    for (int i = 1; i < n - 1; i++) {
        w_lp[i] = (w_hp[i - 1] + w_hp[i] + w_hp[i + 1]) / 3.0f;
    }

    float mean = 0.0f;
    for (int i = 0; i < n; i++) mean += w_lp[i];
    mean /= n;

    float denom = 0.0f;
    for (int i = 0; i < n; i++) {
        w_lp[i] -= mean;
        denom += w_lp[i] * w_lp[i];
    }
    if (denom < 1e-9f) return NAN;

    // Unbiased autocorrelation: each lag is normalised by its own overlap, so
    // longer lags are not penalised in a short window.
    float rValues[RESP_LAG_MAX + 2];
    float bestR = -1.0f;
    int   bestLag = -1;

    for (int lag = RESP_LAG_MIN; lag <= lagMax; lag++) {
        float s = 0.0f;
        for (int i = 0; i < n - lag; i++) s += w_lp[i] * w_lp[i + lag];
        float r = (s / (n - lag)) / (denom / n);
        rValues[lag] = r;
        if (r > bestR) { bestR = r; bestLag = lag; }
    }

    if (bestLag < 0 || bestR < RESP_CORR_MIN) return NAN;

    // A periodic signal also correlates at two and three times its period,
    // and the unbiased estimate can rank those slightly higher. Take the
    // shortest lag that is a local peak and nearly as strong as the best.
    for (int lag = RESP_LAG_MIN + 1; lag < bestLag; lag++) {
        if (rValues[lag] >= rValues[lag - 1] && rValues[lag] >= rValues[lag + 1]
            && rValues[lag] >= 0.85f * bestR) {
            bestLag = lag;
            bestR = rValues[lag];
            break;
        }
    }

    if (bestLag == RESP_LAG_MIN || bestLag == lagMax) return NAN;

    float rBefore = rValues[bestLag - 1];
    float rPeak   = rValues[bestLag];
    float rAfter  = rValues[bestLag + 1];
    float refined = (float)bestLag;
    float curve   = rBefore - 2.0f * rPeak + rAfter;
    if (fabsf(curve) > 1e-6f) {
        refined += 0.5f * (rBefore - rAfter) / curve;
    }
    if (refined < 1.0f) return NAN;

    float rate = 60.0f * RESP_FS / refined;
    if (rate < 5.0f || rate > 45.0f) return NAN;
    return rate;
}

// Median of the last few fused values
float RespirationMonitor::smooth(float value) {
    _hist[_histHead] = value;
    _histHead = (_histHead + 1) % RESP_SMOOTH;
    if (_histN < RESP_SMOOTH) _histN++;

    float s[RESP_SMOOTH];
    for (uint8_t i = 0; i < _histN; i++) s[i] = _hist[i];
    for (uint8_t i = 1; i < _histN; i++) {
        float key = s[i];
        int8_t j = i - 1;
        while (j >= 0 && s[j] > key) { s[j + 1] = s[j]; j--; }
        s[j + 1] = key;
    }
    if (_histN == 2) return 0.5f * (s[0] + s[1]);
    return s[_histN / 2];
}

RespirationResult RespirationMonitor::compute() {
    RespirationResult out;

    const int n = cleanSamples();
    out.windowSec = n / RESP_FS;
    if (n < RESP_MIN_FILL) {
        out.note = (_filled > n) ? "artefact" : "filling";
        // A gap in clean signal means the old trend no longer applies
        if (_filled > n) _histN = 0;
        return out;
    }
    out.preliminary = n < RESP_WINDOW;

    tail(_riiv, n, w_lin);
    out.riiv = estimate(w_lin, n);

    const int nb = (_beatGrid < n) ? _beatGrid : n;
    if (_beatCount >= RESP_MIN_BEATS && nb >= RESP_MIN_FILL) {
        tail(_riav, nb, w_lin);
        out.riav = estimate(w_lin, nb);

        tail(_rifv, nb, w_lin);
        out.rifv = estimate(w_lin, nb);
    }

    // ---- fusion ----
    // Smart Fusion (Karlen 2013) treats the three estimates as independent
    // and rejects the window when their spread exceeds 4 breaths/min. RIAV and
    // RIFV are not independent, though: both come from the same beat
    // detector, so when beat detection is disturbed (low sample rate, motion)
    // they tend to fail together, on the same wrong value, and outvote RIIV.
    // Here RIIV (baseline) must therefore agree with at least one beat-derived
    // estimate. Without RIIV, the two beat-derived estimates are accepted only
    // at a sample rate where beat timing is reliable.
    float values[3];
    uint8_t count = 0;
    uint8_t produced = 0;
    if (!isnan(out.riiv)) produced++;
    if (!isnan(out.riav)) produced++;
    if (!isnan(out.rifv)) produced++;

    if (!isnan(out.riiv)) {
        values[count++] = out.riiv;
        if (!isnan(out.riav) && fabsf(out.riav - out.riiv) <= RESP_AGREE_MAX) values[count++] = out.riav;
        if (!isnan(out.rifv) && fabsf(out.rifv - out.riiv) <= RESP_AGREE_MAX) values[count++] = out.rifv;
    } else if (!isnan(out.riav) && !isnan(out.rifv) && _fsEst >= RESP_FS_BEAT_MIN
               && fabsf(out.riav - out.rifv) <= RESP_AGREE_MAX) {
        values[count++] = out.riav;
        values[count++] = out.rifv;
    }
    out.contributing = count;

    if (count < 2) {
        if (produced < 2) out.note = (_beatCount < RESP_MIN_BEATS) ? "few beats" : "few sources";
        else              out.note = "disagree";
        return out;
    }

    float mean = 0.0f;
    for (uint8_t i = 0; i < count; i++) mean += values[i];
    mean /= count;
    float var = 0.0f;
    for (uint8_t i = 0; i < count; i++) var += (values[i] - mean) * (values[i] - mean);
    out.spread = sqrtf(var / count);

    out.rate = smooth(mean);
    out.valid = true;
    out.note = out.preliminary ? "prelim" : "ok";
    return out;
}