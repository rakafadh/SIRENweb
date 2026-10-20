#include "respiration.h"
#include <math.h>

// Scratch buffers at file scope so the class stays small and nothing large
// lands on the loop task stack.
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
    _gridStartMs = 0;
    _accum = 0;
    _accumN = 0;

    _lastMs = 0;
    _baseline = NAN;
    _env = 0.0f;

    _acPrev1 = _acPrev2 = 0.0f;
    _tPrev1 = _tPrev2 = 0;

    _lastBeatTime = 0.0f;
    _meanInterval = 0.0f;
    _beatAmp = NAN;
    _beatInterval = NAN;
    _beatCount = 0;

    _beatHead = 0;
    _beatFilled = 0;
}

// ---------------------------------------------------------- sample intake ---
void RespirationMonitor::addSample(uint32_t ir, uint32_t nowMs) {
    float value = (float)ir;

    // Adaptive timing. The input rate is measured, never assumed.
    float dt = (_lastMs == 0) ? 0.04f : (nowMs - _lastMs) / 1000.0f;
    if (dt <= 0.0f || dt > 1.0f) dt = 0.04f;
    _lastMs = nowMs;

    // In reflectance PPG the IR count dips at systole, so inverting the
    // baseline-removed signal turns those dips into positive peaks.
    if (isnan(_baseline)) _baseline = value;
    float alphaBase = dt / (0.6f + dt);
    _baseline += alphaBase * (value - _baseline);
    float ac = _baseline - value;

    // Decaying envelope gives a threshold that tracks perfusion
    float alphaEnv = dt / (3.0f + dt);
    if (ac > _env) _env = ac;
    else           _env -= alphaEnv * _env;

    // --- three-point peak test with parabolic refinement ---
    // The peak is tested on the PREVIOUS sample, because a maximum is only
    // known once the next sample has come in lower.
    float threshold = 0.35f * _env;
    if (_env > 20.0f && _tPrev2 != 0) {
        bool isPeak = (_acPrev1 > _acPrev2) && (_acPrev1 >= ac) && (_acPrev1 > threshold);
        if (isPeak) {
            // Refractory keeps the dicrotic notch from registering as a beat.
            float refractory = 300.0f;
            if (_meanInterval > 0.0f && 0.5f * _meanInterval > refractory) {
                refractory = 0.5f * _meanInterval;
            }

            // Fit a parabola through the peak and its two neighbours. The
            // vertex offset is in samples, so scale it by the local sample
            // period to get milliseconds.
            float peakTime = (float)_tPrev1;
            float curve = _acPrev2 - 2.0f * _acPrev1 + ac;
            if (fabsf(curve) > 1e-6f) {
                float delta = 0.5f * (_acPrev2 - ac) / curve;
                if (delta > -1.0f && delta < 1.0f) {
                    float samplePeriod = ((float)nowMs - (float)_tPrev2) * 0.5f;
                    peakTime += delta * samplePeriod;
                }
            }

            if (_lastBeatTime <= 0.0f || (peakTime - _lastBeatTime) >= refractory) {
                registerBeat(peakTime, _acPrev1);
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
            _beatInterval = iv;
            _beatAmp = amplitude;
            _beatCount++;
            _meanInterval = (_meanInterval <= 0.0f)
                          ? iv
                          : _meanInterval + 0.20f * (iv - _meanInterval);
        } else {
            // A gap means beats were missed, so the interval chain is broken.
            // Drop the history rather than average across the hole.
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
    float riivValue;
    if (_accumN > 0) {
        riivValue = (float)(_accum / _accumN);
    } else {
        int prev = (_head - 1 + RESP_WINDOW) % RESP_WINDOW;
        riivValue = (_filled > 0) ? _riiv[prev] : 0.0f;
    }
    _accum = 0;
    _accumN = 0;

    _riiv[_head] = riivValue;
    // Zero-order hold on the beat series. The 0.6 Hz low pass downstream
    // removes the step edges this introduces.
    _riav[_head] = isnan(_beatAmp) ? 0.0f : _beatAmp;
    _rifv[_head] = isnan(_beatInterval) ? 0.0f : _beatInterval;

    _head = (_head + 1) % RESP_WINDOW;
    if (_filled < RESP_WINDOW) _filled++;
}

// -------------------------------------------------------------- heart rate --
BeatStats RespirationMonitor::beatRate() const {
    BeatStats s;
    if (_beatFilled < 5) return s;

    // Linearise the ring into chronological order
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

    // Median interval, used as the reference for trimming
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

    // Trimmed mean. An interval far from the median means a missed or a
    // spurious beat, and averaging it in would drag the result.
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
void RespirationMonitor::linearise(const float *ring, float *out) const {
    int start = (_head - _filled + RESP_WINDOW) % RESP_WINDOW;
    for (int i = 0; i < _filled; i++) {
        out[i] = ring[(start + i) % RESP_WINDOW];
    }
}

float RespirationMonitor::estimate(const float *series) const {
    const int n = _filled;
    if (n < RESP_MIN_FILL) return NAN;

    // High pass at roughly 0.1 Hz by subtracting a 10 s moving average
    const int hpWidth = 40;
    for (int i = 0; i < n; i++) {
        int a = i - hpWidth / 2; if (a < 0) a = 0;
        int b = i + hpWidth / 2 + 1; if (b > n) b = n;
        float sum = 0.0f;
        for (int j = a; j < b; j++) sum += series[j];
        w_hp[i] = series[i] - (sum / (b - a));
    }

    // Low pass at roughly 0.6 Hz with a 3-point moving average, written to a
    // second buffer so each output uses unfiltered neighbours.
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
    if (denom < 1e-6f) return NAN;

    float rValues[RESP_LAG_MAX + 2];
    float bestR = -1.0f;
    int   bestLag = -1;

    for (int lag = RESP_LAG_MIN; lag <= RESP_LAG_MAX; lag++) {
        float s = 0.0f;
        for (int i = 0; i < n - lag; i++) s += w_lp[i] * w_lp[i + lag];
        float r = s / denom;
        rValues[lag] = r;
        if (r > bestR) { bestR = r; bestLag = lag; }
    }

    if (bestLag < 0 || bestR < RESP_CORR_MIN) return NAN;

    // A peak pinned to either end of the search range is usually an artefact
    // rather than a real respiratory period.
    if (bestLag == RESP_LAG_MIN || bestLag == RESP_LAG_MAX) return NAN;

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

RespirationResult RespirationMonitor::compute() {
    RespirationResult out;

    if (_filled < RESP_MIN_FILL) {
        out.note = "filling";
        return out;
    }

    linearise(_riiv, w_lin);
    out.riiv = estimate(w_lin);

    // RIAV and RIFV only mean something once enough beats have populated the
    // series. Before that they are dominated by the startup step.
    if (_beatCount >= RESP_MIN_BEATS) {
        linearise(_riav, w_lin);
        out.riav = estimate(w_lin);

        linearise(_rifv, w_lin);
        out.rifv = estimate(w_lin);
    }

    float values[3];
    uint8_t count = 0;
    if (!isnan(out.riiv)) values[count++] = out.riiv;
    if (!isnan(out.riav)) values[count++] = out.riav;
    if (!isnan(out.rifv)) values[count++] = out.rifv;
    out.contributing = count;

    if (count < 2) {
        out.note = (_beatCount < RESP_MIN_BEATS) ? "few beats" : "few sources";
        return out;
    }

    float mean = 0.0f;
    for (uint8_t i = 0; i < count; i++) mean += values[i];
    mean /= count;

    float var = 0.0f;
    for (uint8_t i = 0; i < count; i++) var += (values[i] - mean) * (values[i] - mean);
    out.spread = sqrtf(var / count);

    if (out.spread > RESP_AGREE_MAX) {
        out.note = "disagree";
        return out;
    }

    out.rate = mean;
    out.valid = true;
    out.note = "ok";
    return out;
}