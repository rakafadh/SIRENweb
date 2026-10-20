/*
 * SIREN - signal conditioning
 *
 * PpgFilter cleans the MAX30102 red and IR channels before the Maxim
 * algorithm sees them. It deliberately preserves the DC level, because SpO2
 * is computed from (AC_red/DC_red) / (AC_ir/DC_ir) and a plain bandpass
 * would remove the denominator.
 *
 * Chain:
 *   1. 3-point median            impulse and dropout rejection
 *   2. first-order EMA           DC estimate, about 2 s time constant
 *   3. 2nd-order Butterworth HP  0.5 Hz, removes baseline wander
 *   4. 2nd-order Butterworth LP  4.0 Hz, removes noise above 240 bpm
 *   5. recombine as DC + AC      Maxim still gets a usable DC level
 *
 * Butterworth is chosen for its flat passband. SpO2 depends on the amplitude
 * ratio between the two channels, so passband ripple would bias the result.
 * Both channels must run identical filters, which makes any residual gain
 * error cancel in the division.
 *
 * The respiration module must keep receiving RAW samples. RIIV lives at 0.1
 * to 0.6 Hz, which the 0.5 Hz highpass here would destroy.
 *
 * TempFilter conditions the MLX90614. Temperature changes slowly, so heavy
 * smoothing costs almost nothing in responsiveness and buys a lot of noise
 * rejection.
 */

#pragma once

#include <Arduino.h>

// -------------------------------------------------------------- biquad ------
// Direct form II transposed, RBJ cookbook coefficients.
class Biquad {
public:
    void  lowpass(float fs, float fc, float q = 0.70710678f);
    void  highpass(float fs, float fc, float q = 0.70710678f);
    void  reset();
    float process(float x);

private:
    float _b0 = 1, _b1 = 0, _b2 = 0, _a1 = 0, _a2 = 0;
    float _z1 = 0, _z2 = 0;
};

// ----------------------------------------------------------------- ppg ------
class PpgFilter {
public:
    // Call once at startup, and again if the measured sample rate moves
    // significantly. Resets filter state.
    void configure(float fs);

    // Feed one raw channel sample, get the conditioned value back.
    float process(float raw);

    float dc() const      { return isnan(_dc) ? 0.0f : _dc; }
    float ac() const      { return _acFiltered; }
    bool  settled() const { return _count > (uint32_t)(_fs * 3.0f); }
    void  reset();

private:
    float  _fs = 25.0f;
    Biquad _hp, _lp;

    float   _hist[3] = {0, 0, 0};
    uint8_t _histN = 0;

    float    _dc = NAN;
    float    _dcAlpha = 0.02f;
    float    _acFiltered = 0.0f;
    uint32_t _count = 0;

    static float median3(float a, float b, float c);
};

// ---------------------------------------------------------------- temp ------
class TempFilter {
public:
    // tau is the smoothing time constant in seconds. reject is the largest
    // jump from the current estimate that will be accepted, in degrees C.
    //
    // The default reject is deliberately wide. It exists to discard I2C
    // garbage, not to police real temperature changes: swinging the sensor
    // from a room onto skin is a genuine 3 to 4 degree step and must be
    // allowed through. A tight gate here silently hides the measurement.
    void configure(float tauSeconds = 3.0f, float reject = 8.0f);

    // Feed one reading with its millis() timestamp. Returns the smoothed
    // estimate, or NAN before the first valid sample.
    float process(float celsius, uint32_t nowMs);

    float    value() const    { return _est; }
    uint32_t rejected() const { return _rejected; }
    bool     settled() const  { return _count > 4; }
    void     reset();

private:
    static const uint8_t TEMP_MEDIAN_N = 3;

    float    _tau = 3.0f;
    float    _reject = 8.0f;
    float    _est = NAN;
    float    _hist[TEMP_MEDIAN_N] = {0, 0, 0};
    uint8_t  _histN = 0;
    uint32_t _lastMs = 0;
    uint32_t _count = 0;
    uint32_t _rejected = 0;

    static float medianN(const float *v, uint8_t n);
};