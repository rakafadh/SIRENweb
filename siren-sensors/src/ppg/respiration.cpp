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
    _aa1 = _aa2 = NAN;
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
    _lastValidMs = 0;
    _agreeRate = NAN;
    _agreeMs = 0;
}

void RespirationMonitor::markDiscontinuity() {
    // Drop the partial grid sample, which straddles the step, and splice the
    // next one onto the previous level.
    _accum = 0;
    _accumN = 0;
    _spliceNext = true;
    _aa1 = _aa2 = NAN;      // restart the anti-alias filter at the new level

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

    // --- anti-alias low pass, then accumulate onto the uniform grid ---
    // Two first-order stages at RESP_AA_HZ, started at rest on the first
    // sample (and again after an LED step). Without it the pulse harmonics
    // fold into RIIV when the heart rate is high.
    if (isnan(_aa1)) { _aa1 = value; _aa2 = value; }
    const float aAa = dt / (dt + 1.0f / (2.0f * (float)M_PI * RESP_AA_HZ));
    _aa1 += aAa * (value - _aa1);
    _aa2 += aAa * (_aa1 - _aa2);

    if (_gridStartMs == 0) _gridStartMs = nowMs;
    _accum += _aa2;
    _accumN++;

    const uint32_t gridMs = (uint32_t)(1000.0f / RESP_FS + 0.5f);
    while ((int32_t)(nowMs - _gridStartMs) >= (int32_t)gridMs) {
        pushGrid();
        _gridStartMs += gridMs;
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

// Zero-phase second-order Butterworth high pass at RESP_HP_HZ on the grid
static void highPassZeroPhase(float *x, int n) {
    static bool init = false;
    static float b0, b1, b2, a1, a2;
    if (!init) {
        const float K = tanf((float)M_PI * RESP_HP_HZ / RESP_FS);
        const float q = 0.70710678f;
        const float norm = 1.0f / (1.0f + K / q + K * K);
        b0 = norm; b1 = -2.0f * norm; b2 = norm;
        a1 = 2.0f * (K * K - 1.0f) * norm;
        a2 = (1.0f - K / q + K * K) * norm;
        init = true;
    }
    for (int pass = 0; pass < 2; pass++) {
        float x1 = x[0], x2 = x[0], y1 = 0.0f, y2 = 0.0f;   // start at rest on the first sample
        for (int i = 0; i < n; i++) {
            float xi = x[i];
            float y = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
            x2 = x1; x1 = xi; y2 = y1; y1 = y;
            x[i] = y;
        }
        for (int i = 0; i < n / 2; i++) { float t = x[i]; x[i] = x[n - 1 - i]; x[n - 1 - i] = t; }
    }
}

// Power at frequency f (Hz) of the n-sample series on the grid (Goertzel)
static float binPower(const float *x, int n, float f) {
    const float w = 2.0f * (float)M_PI * f / RESP_FS;
    const float c = 2.0f * cosf(w);
    float s1 = 0.0f, s2 = 0.0f;
    for (int i = 0; i < n; i++) {
        float s0 = x[i] + c * s1 - s2;
        s2 = s1; s1 = s0;
    }
    return s1 * s1 + s2 * s2 - c * s1 * s2;
}

static const int RESP_NBINS = 573;   // bins from RESP_F_SPEC to RESP_F_MAX_ABS at RESP_F_STEP (0.07-1.50 Hz)
static float w_pow[RESP_NBINS];

static inline bool isLocalMax(const float *p, int k) { return p[k] >= p[k - 1] && p[k] >= p[k + 1]; }

float RespirationMonitor::estimate(const float *series, int n, float fMax, float *quality,
                                   float *lowRate, bool *lowDominant) const {
    if (quality) *quality = 0.0f;
    if (lowRate) *lowRate = NAN;
    if (lowDominant) *lowDominant = false;
    if (n < RESP_MIN_FILL) return NAN;

    // Remove the mean and linear trend, high pass, Hann window
    float sx = 0.0f, sxx = 0.0f, sy = 0.0f, sxy = 0.0f;
    for (int i = 0; i < n; i++) { sx += i; sxx += (float)i * i; sy += series[i]; sxy += i * series[i]; }
    const float den = n * sxx - sx * sx;
    const float slope = (den != 0.0f) ? (n * sxy - sx * sy) / den : 0.0f;
    const float icpt = (sy - slope * sx) / n;
    for (int i = 0; i < n; i++) w_hp[i] = series[i] - (icpt + slope * i);
    highPassZeroPhase(w_hp, n);
    float energy = 0.0f;
    for (int i = 0; i < n; i++) {
        float h = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * i / (n - 1));
        w_lp[i] = w_hp[i] * h;
        energy += w_lp[i] * w_lp[i];
    }
    if (energy < 1e-12f) return NAN;

    // Spectrum from RESP_F_SPEC to fMax
    if (fMax > RESP_F_MAX_ABS) fMax = RESP_F_MAX_ABS;
    if (fMax < RESP_F_MAX_MIN) fMax = RESP_F_MAX_MIN;
    int nb = (int)((fMax - RESP_F_SPEC) / RESP_F_STEP + 1.5f);
    if (nb > RESP_NBINS) nb = RESP_NBINS;
    const int kLow = (int)((RESP_F_LOW - RESP_F_SPEC) / RESP_F_STEP + 0.5f);  // slow-breathing band start
    const int kMin = (int)((RESP_F_MIN - RESP_F_SPEC) / RESP_F_STEP + 0.5f);  // breathing band start
    for (int k = 0; k < nb; k++) w_pow[k] = binPower(w_lp, n, RESP_F_SPEC + k * RESP_F_STEP);

    // Breathing band: strongest interior local maximum
    float mean = 0.0f;
    for (int k = kMin; k < nb; k++) mean += w_pow[k];
    mean /= (nb - kMin);
    int best = -1;
    for (int k = kMin + 1; k < nb - 1; k++) {
        if (isLocalMax(w_pow, k) && (best < 0 || w_pow[k] > w_pow[best])) best = k;
    }
    // Strongest real peak in the band (a local maximum, so the slope of
    // leakage from a peak just below the band does not count)
    const float bandMax = (best >= 0) ? w_pow[best] : 0.0f;

    // Slow-breathing band 0.09-0.14 Hz: its own interior peak, and whether it dominates
    {
        int lb = -1;
        for (int k = (kLow > 1 ? kLow : 1); k < kMin; k++) {
            if (isLocalMax(w_pow, k) && (lb < 0 || w_pow[k] > w_pow[lb])) lb = k;
        }
        if (lb >= 0) {
            if (lowRate) *lowRate = 60.0f * (RESP_F_SPEC + lb * RESP_F_STEP);
            if (lowDominant) *lowDominant = w_pow[lb] > 2.0f * bandMax && w_pow[lb] > RESP_PEAK_MIN * mean;
        }
    }

    if (best < 0 || mean <= 0.0f) return NAN;

    // Mayer guard (v4): a peak at 0.07-0.14 Hz at least as strong as the band
    // peak, close enough that the band peak may be its spread, pushes the
    // search past it.
    int kStart = kMin + 1;
    {
        int gb = -1;
        for (int k = 1; k < kMin; k++) {
            if (isLocalMax(w_pow, k) && (gb < 0 || w_pow[k] > w_pow[gb])) gb = k;
        }
        const float guardHz = RESP_GUARD_RES * RESP_FS / n;
        const int guardBins = (int)(guardHz / RESP_F_STEP + 0.5f);
        if (gb >= 0 && w_pow[gb] >= w_pow[best] && best - gb < guardBins) {
            kStart = gb + guardBins;
            best = -1;
            for (int k = kStart; k < nb - 1; k++) {
                if (isLocalMax(w_pow, k) && (best < 0 || w_pow[k] > w_pow[best])) best = k;
            }
            if (best < 0) return NAN;
        }
    }

    // Prefer the fundamental: a clear local peak near half the frequency
    const float fBest = RESP_F_SPEC + best * RESP_F_STEP;
    const int kHalf = (int)((0.5f * fBest - RESP_F_SPEC) / RESP_F_STEP + 0.5f);
    if (kHalf >= kStart && kHalf < nb - 1) {
        int kh = kHalf;
        for (int d = -2; d <= 2; d++) {
            int k = kHalf + d;
            if (k >= kStart && k < nb - 1 && w_pow[k] > w_pow[kh]) kh = k;
        }
        if (isLocalMax(w_pow, kh) && w_pow[kh] >= 0.5f * w_pow[best]) best = kh;
    }
    const float q = w_pow[best] / mean;
    if (quality) *quality = q;
    if (q < RESP_PEAK_MIN) return NAN;

    // Parabolic refinement between bins
    float f = RESP_F_SPEC + best * RESP_F_STEP;
    const float ym = w_pow[best - 1], y0 = w_pow[best], yp = w_pow[best + 1];
    const float curve = ym - 2.0f * y0 + yp;
    if (fabsf(curve) > 1e-12f) {
        float d = 0.5f * (ym - yp) / curve;
        if (d > -1.0f && d < 1.0f) f += d * RESP_F_STEP;
    }
    const float rate = 60.0f * f;
    if (rate < 60.0f * RESP_F_MIN || rate > 60.0f * fMax) return NAN;
    return rate;
}

bool RespirationMonitor::pulseDoubled(int n, float hrHz) const {
    if (hrHz <= 0.0f || 2.0f * hrHz > 0.45f * RESP_FS) return false;
    float p1 = 0.0f, p2 = 0.0f;
    for (int d = -4; d <= 4; d++) {                       // +-4 % around each, for HRV
        p1 = fmaxf(p1, binPower(w_lp, n, hrHz * (1.0f + 0.01f * d)));
        p2 = fmaxf(p2, binPower(w_lp, n, 2.0f * hrHz * (1.0f + 0.01f * d)));
    }
    return p2 > RESP_DOUBLE_REL * p1;
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
        if (_filled > n) { _histN = 0; _histHead = 0; }
        return out;
    }
    out.preliminary = n < RESP_WINDOW;

    // Search ceiling: half the heart rate (beat-derived signals are sampled
    // once per beat), never below RESP_F_MAX_MIN, at most RESP_F_MAX_ABS.
    float hrHz = 0.0f;
    BeatStats bs = beatRate();
    if (bs.valid) hrHz = bs.bpm / 60.0f;
    else if (_meanInterval > 0.0f) hrHz = 1000.0f / _meanInterval;
    float fMax = 0.5f * hrHz;
    if (fMax < RESP_F_MAX_MIN) fMax = RESP_F_MAX_MIN;
    if (fMax > RESP_F_MAX_ABS) fMax = RESP_F_MAX_ABS;
    out.rateMax = 60.0f * fMax;

    float q[3] = {0.0f, 0.0f, 0.0f}, low[3] = {NAN, NAN, NAN};
    bool lowDom[3] = {false, false, false};
    tail(_riiv, n, w_lin);
    out.riiv = estimate(w_lin, n, fMax, &q[0], &low[0], &lowDom[0]);
    const bool doubled = (_fsEst >= RESP_FS_BEAT_MIN) && pulseDoubled(n, hrHz);   // w_lp still holds RIIV
    const int nb = (_beatGrid < n) ? _beatGrid : n;
    if (_beatCount >= RESP_MIN_BEATS && nb >= RESP_MIN_FILL) {
        tail(_riav, nb, w_lin);
        out.riav = estimate(w_lin, nb, fMax, &q[1], &low[1], &lowDom[1]);
        tail(_rifv, nb, w_lin);
        out.rifv = estimate(w_lin, nb, fMax, &q[2], &low[2], &lowDom[2]);
    }

    // ---- fusion (v3) ----
    // Any two of the three that agree within RESP_AGREE_MAX: all three when
    // they all agree, else the agreeing pair with the best summed quality.
    // RIAV + RIFV share one beat detector and fail together at low sample
    // rates, so that pair alone needs RESP_FS_BEAT_MIN.
    const float est[3] = {out.riiv, out.riav, out.rifv};
    float values[3];
    uint8_t count = 0;
    uint8_t produced = 0;
    for (int i = 0; i < 3; i++) if (!isnan(est[i])) produced++;

    // Fast breathing (v4): beat-derived estimates that sit on an alias of a
    // strong, fast RIIV are dropped, and RIIV may stand alone.
    float estF[3] = {est[0], est[1], est[2]};
    bool fast = false;
    const float hrMin = 60.0f * hrHz;
    if (!isnan(est[0]) && hrMin > 0.0f && est[0] > RESP_FAST_HR_FRAC * hrMin && q[0] >= RESP_FAST_QMIN) {
        const float R = est[0];
        const float aliases[2] = {fabsf(hrMin - R), fabsf(hrMin - 2.0f * R)};
        int dropped = 0, agree = 0;
        for (int i = 1; i < 3; i++) {
            if (isnan(est[i])) continue;
            if (fabsf(est[i] - R) <= RESP_AGREE_MAX) { agree++; continue; }
            for (float a : aliases) {
                if (fabsf(est[i] - a) <= RESP_ALIAS_TOL) { estF[i] = NAN; dropped++; break; }
            }
        }
        if (dropped > 0 && agree == 0) fast = true;
    }
    // Slow breathing below the band first: all three put a dominant peak in
    // 0.09-0.14 Hz and agree. Their in-band peaks are then harmonics, which
    // the pairs below would otherwise accept.
    if (lowDom[0] && lowDom[1] && lowDom[2]) {
        float lo = fminf(low[0], fminf(low[1], low[2]));
        float hi = fmaxf(low[0], fmaxf(low[1], low[2]));
        if (hi - lo <= RESP_LOW_AGREE) {
            values[0] = low[0]; values[1] = low[1]; values[2] = low[2]; count = 3;
            out.note = "slow";
        }
    }
    // Every other beat missed: the heart rate, the ceiling and the beat
    // signals are all wrong. No number, count manually. (At 10 Hz the grid
    // fill is uneven, so the check runs at 20 Hz or more only.)
    if (doubled) {
        out.note = "fast?";
        return out;
    }
    if (count == 0 && fast) {
        values[0] = est[0]; count = 1;
        out.note = "fast";
    }
    if (count == 0 && produced == 3) {
        float lo = fminf(est[0], fminf(est[1], est[2]));
        float hi = fmaxf(est[0], fmaxf(est[1], est[2]));
        if (hi - lo <= RESP_AGREE_MAX) { values[0] = est[0]; values[1] = est[1]; values[2] = est[2]; count = 3; }
    }
    if (count == 0) {
        const int pairs[3][2] = {{0, 1}, {0, 2}, {1, 2}};
        float bestQ = -1.0f;
        for (int p = 0; p < 3; p++) {
            int a = pairs[p][0], b = pairs[p][1];
            if (isnan(estF[a]) || isnan(estF[b]) || fabsf(estF[a] - estF[b]) > RESP_AGREE_MAX) continue;
            if (a == 1 && b == 2 && _fsEst < RESP_FS_BEAT_MIN) continue;
            if (q[a] + q[b] > bestQ) { bestQ = q[a] + q[b]; values[0] = estF[a]; values[1] = estF[b]; count = 2; }
        }
    }
    // Tracking (v4): nothing above was accepted, but one signal is within RESP_TRACK_TOL of
    // the rate two signals agreed on less than RESP_TRACK_MS ago.
    bool tracking = false;
    if (count == 0 && !isnan(_agreeRate) && (uint32_t)(_lastMs - _agreeMs) <= RESP_TRACK_MS) {
        int pick = -1;
        for (int i = 0; i < 3; i++) {
            if (isnan(estF[i]) || fabsf(estF[i] - _agreeRate) > RESP_TRACK_TOL) continue;
            if (pick < 0 || q[i] > q[pick]) pick = i;
        }
        if (pick >= 0) { values[0] = estF[pick]; count = 1; tracking = true; }
    }

    out.contributing = count;
    if (count == 0) {
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

    // A gap with no RR output: the old values no longer describe the breathing.
    // (_histHead must restart too: smooth() reads _hist[0.._histN-1], and v3
    // reset only _histN, so a stale slot was read back after every gap.)
    if (_lastValidMs == 0 || (uint32_t)(_lastMs - _lastValidMs) > RESP_GAP_RESET_MS) { _histN = 0; _histHead = 0; }
    _lastValidMs = _lastMs;

    out.rate = smooth(mean);
    out.valid = true;
    if (tracking) {
        out.note = "tracking";
    } else if (!fast) {                 // only real agreement seeds tracking
        _agreeRate = out.rate;
        _agreeMs = _lastMs;
    }
    if (out.note[0] == '\0') out.note = out.preliminary ? "prelim" : "ok";
    return out;
}