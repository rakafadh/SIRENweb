#include "filters.h"
#include <math.h>

// -------------------------------------------------------------- biquad ------
void Biquad::reset() {
    _z1 = 0.0f;
    _z2 = 0.0f;
}

void Biquad::lowpass(float fs, float fc, float q) {
    if (fc >= fs * 0.5f) fc = fs * 0.45f;          // stay below Nyquist
    float w0 = 2.0f * PI * fc / fs;
    float cosw = cosf(w0);
    float alpha = sinf(w0) / (2.0f * q);

    float b0 = (1.0f - cosw) * 0.5f;
    float b1 = 1.0f - cosw;
    float b2 = b0;
    float a0 = 1.0f + alpha;
    float a1 = -2.0f * cosw;
    float a2 = 1.0f - alpha;

    _b0 = b0 / a0; _b1 = b1 / a0; _b2 = b2 / a0;
    _a1 = a1 / a0; _a2 = a2 / a0;
    reset();
}

void Biquad::highpass(float fs, float fc, float q) {
    if (fc >= fs * 0.5f) fc = fs * 0.45f;
    float w0 = 2.0f * PI * fc / fs;
    float cosw = cosf(w0);
    float alpha = sinf(w0) / (2.0f * q);

    float b0 = (1.0f + cosw) * 0.5f;
    float b1 = -(1.0f + cosw);
    float b2 = b0;
    float a0 = 1.0f + alpha;
    float a1 = -2.0f * cosw;
    float a2 = 1.0f - alpha;

    _b0 = b0 / a0; _b1 = b1 / a0; _b2 = b2 / a0;
    _a1 = a1 / a0; _a2 = a2 / a0;
    reset();
}

float Biquad::process(float x) {
    float y = _b0 * x + _z1;
    _z1 = _b1 * x - _a1 * y + _z2;
    _z2 = _b2 * x - _a2 * y;
    return y;
}

// ----------------------------------------------------------------- ppg ------
float PpgFilter::median3(float a, float b, float c) {
    float t;
    if (a > b) { t = a; a = b; b = t; }
    if (b > c) { t = b; b = c; c = t; }
    if (a > b) { t = a; a = b; b = t; }
    return b;
}

void PpgFilter::configure(float fs) {
    if (fs < 5.0f)  fs = 5.0f;
    if (fs > 400.0f) fs = 400.0f;
    _fs = fs;

    // 0.5 Hz high pass removes baseline wander below 30 bpm.
    // 4.0 Hz low pass removes noise above 240 bpm.
    _hp.highpass(_fs, 0.5f);
    _lp.lowpass(_fs, 4.0f);

    // First-order EMA with a 2 s time constant for the DC estimate
    _dcAlpha = 1.0f - expf(-1.0f / (_fs * 2.0f));

    reset();
}

void PpgFilter::reset() {
    _hp.reset();
    _lp.reset();
    _histN = 0;
    _dc = NAN;
    _acFiltered = 0.0f;
    _count = 0;
}

float PpgFilter::process(float raw) {
    // --- stage 1, 3-point median ---
    _hist[2] = _hist[1];
    _hist[1] = _hist[0];
    _hist[0] = raw;
    if (_histN < 3) _histN++;
    float x = (_histN < 3) ? raw : median3(_hist[0], _hist[1], _hist[2]);

    // --- stage 2, DC tracking ---
    if (isnan(_dc)) _dc = x;
    _dc += _dcAlpha * (x - _dc);

    // --- stages 3 and 4, bandpass on the AC path only ---
    float ac = x - _dc;
    ac = _hp.process(ac);
    ac = _lp.process(ac);
    _acFiltered = ac;

    _count++;

    // --- stage 5, recombine so the DC level survives for SpO2 ---
    return _dc + ac;
}

// ---------------------------------------------------------------- temp ------
float TempFilter::medianN(const float *v, uint8_t n) {
    float s[TEMP_MEDIAN_N];
    for (uint8_t i = 0; i < n; i++) s[i] = v[i];
    for (uint8_t i = 1; i < n; i++) {
        float key = s[i];
        int8_t j = i - 1;
        while (j >= 0 && s[j] > key) { s[j + 1] = s[j]; j--; }
        s[j + 1] = key;
    }
    return s[n / 2];
}

void TempFilter::configure(float tauSeconds, float reject) {
    _tau = tauSeconds;
    _reject = reject;
    reset();
}

void TempFilter::reset() {
    _est = NAN;
    _histN = 0;
    _lastMs = 0;
    _count = 0;
    _rejected = 0;
}

float TempFilter::process(float celsius, uint32_t nowMs) {
    // Discard values the sensor cannot plausibly be reading off a person or
    // a room. These appear on I2C glitches as 0x0000 or 0xFFFF.
    if (isnan(celsius) || celsius < -20.0f || celsius > 80.0f) {
        _rejected++;
        return _est;
    }

    // --- 3-point median, rejects isolated spikes ---
    // Deliberately short. A longer median delays a genuine step by however
    // many samples it spans, which at a 2.5 s loop interval is seconds.
    for (int8_t i = TEMP_MEDIAN_N - 1; i > 0; i--) _hist[i] = _hist[i - 1];
    _hist[0] = celsius;
    if (_histN < TEMP_MEDIAN_N) _histN++;
    float x = medianN(_hist, _histN);

    if (isnan(_est)) {
        _est = x;
        _lastMs = nowMs;
        _count = 1;
        return _est;
    }

    // --- outlier gate ---
    // Wide by design. Only I2C garbage should land here; a real move from
    // room temperature onto skin must pass through.
    if (fabsf(x - _est) > _reject) {
        _rejected++;
        // Two consecutive rejections means the world really did move, so
        // follow it rather than staying locked on a stale value.
        if (_rejected >= 2) {
            _est = x;
            _rejected = 0;
        }
        _lastMs = nowMs;
        return _est;
    }
    _rejected = 0;

    // --- time-aware EMA, correct even with an irregular call interval ---
    float dt = (_lastMs == 0) ? 1.0f : (nowMs - _lastMs) / 1000.0f;
    if (dt <= 0.0f || dt > 30.0f) dt = 1.0f;
    _lastMs = nowMs;

    float alpha = 1.0f - expf(-dt / _tau);
    _est += alpha * (x - _est);
    _count++;
    return _est;
}