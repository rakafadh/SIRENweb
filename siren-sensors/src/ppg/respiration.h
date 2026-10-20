/*
 * SIREN - beat detection, heart rate and PPG-derived respiration rate
 *
 * Beat detection runs a three-point peak test on the inverted, baseline-
 * removed IR signal, with parabolic interpolation across the peak and its
 * two neighbours. The interpolation matters: without it a beat time can only
 * land on a sample boundary, and at a low sample rate that quantisation
 * dominates the heart rate error.
 *
 * Heart rate is then computed from a trimmed mean of beat-to-beat intervals
 * measured in milliseconds, not in samples. Averaging many intervals spreads
 * the residual timing error across the whole window rather than letting one
 * rounded interval set the answer.
 *
 * Respiration extracts three independent modulations and fuses them:
 *
 *   RIIV  respiratory induced intensity variation
 *         Intrathoracic pressure changes venous return, which moves the PPG
 *         baseline. Taken from the low-frequency IR envelope.
 *
 *   RIAV  respiratory induced amplitude variation
 *         Stroke volume varies across the breath, so beat height oscillates.
 *
 *   RIFV  respiratory induced frequency variation
 *         Respiratory sinus arrhythmia, from the beat interval series.
 *
 * Each series is resampled to a uniform 4 Hz grid, bandpassed to 0.1 - 0.6 Hz
 * (6 - 36 breaths/min), and its dominant period found by autocorrelation. The
 * estimates are fused only if they agree, because disagreement means the
 * signal is too corrupted to trust (Karlen et al., IEEE TBME 2013).
 *
 * This class must be fed RAW samples. RIIV lives at 0.1 to 0.6 Hz, which the
 * PPG bandpass in filters.h would remove.
 */

#pragma once

#include <Arduino.h>

static const float RESP_FS         = 4.0f;   // grid rate, Hz
static const int   RESP_WINDOW     = 240;    // 60 s at 4 Hz
static const int   RESP_MIN_FILL   = 120;    // 30 s before the first estimate
static const int   RESP_LAG_MIN    = 7;      // 0.57 Hz, about 34 breaths/min
static const int   RESP_LAG_MAX    = 40;     // 0.10 Hz, about 6 breaths/min
static const float RESP_CORR_MIN   = 0.25f;  // autocorrelation acceptance
static const float RESP_AGREE_MAX  = 4.0f;   // max spread between estimates
static const uint32_t RESP_MIN_BEATS = 15;   // before trusting RIAV and RIFV

static const uint8_t BEAT_RING     = 16;     // beat timestamps retained
static const float   BEAT_IV_MIN   = 250.0f; // ms, 240 bpm ceiling
static const float   BEAT_IV_MAX   = 2200.0f;// ms, 27 bpm floor
static const float   BEAT_TRIM     = 0.30f;  // reject intervals this far off median

struct BeatStats {
    bool    valid     = false;
    float   bpm       = NAN;
    float   sdnn      = NAN;   // ms, interval standard deviation
    uint8_t intervals = 0;     // how many intervals survived trimming
};

struct RespirationResult {
    bool     valid        = false;
    float    rate         = NAN;   // fused, breaths per minute
    float    riiv         = NAN;
    float    riav         = NAN;
    float    rifv         = NAN;
    float    spread       = NAN;   // standard deviation of contributors
    uint8_t  contributing = 0;
    const char *note      = "";
};

class RespirationMonitor {
public:
    void begin();
    void reset();

    // Feed every RAW IR sample as it arrives, with its millis() timestamp.
    // Handles arbitrary and drifting input sample rates internally.
    void addSample(uint32_t ir, uint32_t nowMs);

    bool     ready() const       { return _filled >= RESP_MIN_FILL; }
    float    fillSeconds() const { return _filled / RESP_FS; }
    uint32_t beats() const       { return _beatCount; }

    // Heart rate from beat timing. Independent of the Maxim algorithm and
    // free of its integer-sample quantisation.
    BeatStats beatRate() const;

    RespirationResult compute();

private:
    // --- respiration grids ---
    float _riiv[RESP_WINDOW];
    float _riav[RESP_WINDOW];
    float _rifv[RESP_WINDOW];
    int   _head   = 0;
    int   _filled = 0;

    uint32_t _gridStartMs = 0;
    double   _accum       = 0;
    uint32_t _accumN      = 0;

    // --- beat detection ---
    uint32_t _lastMs   = 0;
    float    _baseline = NAN;
    float    _env      = 0.0f;

    float    _acPrev1 = 0.0f, _acPrev2 = 0.0f;
    uint32_t _tPrev1  = 0,    _tPrev2  = 0;

    float    _lastBeatTime  = 0.0f;   // ms, fractional
    float    _meanInterval  = 0.0f;   // ms, running estimate for refractory
    float    _beatAmp       = NAN;
    float    _beatInterval  = NAN;
    uint32_t _beatCount     = 0;

    float   _beatTimes[BEAT_RING];
    uint8_t _beatHead   = 0;
    uint8_t _beatFilled = 0;

    void  registerBeat(float timeMs, float amplitude);
    void  pushGrid();
    void  linearise(const float *ring, float *out) const;
    float estimate(const float *series) const;
};