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
 * measured in milliseconds, not in samples.
 *
 * Respiration extracts three independent modulations and fuses them:
 *
 *   RIIV  respiratory induced intensity variation (PPG baseline)
 *   RIAV  respiratory induced amplitude variation (beat height)
 *   RIFV  respiratory induced frequency variation (beat interval, RSA)
 *
 * Each series is resampled to a uniform 4 Hz grid, detrended, low passed,
 * and its dominant period found by autocorrelation. The fused value is the
 * median of the last three outputs, which only removes single-window
 * glitches. (Charlton et al., Physiol Meas 2016, found heavier temporal
 * smoothing across windows trades precision for availability.)
 *
 * ---------------------------------------------------------------------------
 * v2 changes, aimed at time-to-first-estimate, recovery time and safety
 * ---------------------------------------------------------------------------
 *  1. Progressive window. A preliminary estimate once 16 s of clean signal
 *     exist, growing to 32 s, the window length used in the PPG respiratory
 *     rate benchmarks (Charlton 2016; Pimentel 2017; Zhang 2020). The longest
 *     period searched is capped at half the window, so an estimate always
 *     spans at least two breaths. Unbiased autocorrelation, with the shortest
 *     strong peak preferred, so a short window does not favour 2x the period.
 *
 *  2. No reset on an LED gain change. RIIV is stored as log intensity and the
 *     step is spliced out as an offset; RIAV is pulse amplitude relative to
 *     the DC level (AC/DC), which does not depend on LED current.
 *
 *  3. Artefacts are masked, not reset. Flagged grid samples are excluded and
 *     estimation uses the clean signal after the most recent flag. Beat
 *     tracking restarts after an artefact and skips three warm-up beats.
 *     History is cleared only when the finger is lost (reset()).
 *
 *  4. Beat detection on a two-stage 0.5 Hz high pass instead of a slow
 *     baseline follower, so the respiratory baseline swing (RIIV) no longer
 *     pushes low beats under the threshold. The beat series start at the
 *     first beat instead of carrying a startup step through the window.
 *
 *  5. Fusion anchored on RIIV. Smart Fusion (Karlen et al., IEEE TBME 2013)
 *     treats RIIV, RIAV and RIFV as independent. RIAV and RIFV share one beat
 *     detector and fail together, often on the same wrong value (at 10 Hz,
 *     half or a third of the true rate). Here RIIV must agree within
 *     4 breaths/min with at least one beat-derived estimate; a conflict gives
 *     no output rather than a wrong one. Without RIIV, the two beat-derived
 *     estimates are accepted only at 20 Hz or more.
 *
 *  6. compute() is cheap and is meant to be called about once per second.
 *
 * This class must be fed RAW samples. RIIV lives at 0.1 to 0.6 Hz, which the
 * PPG bandpass in filters.h would remove.
 */

#pragma once

#include <Arduino.h>

static const float RESP_FS          = 4.0f;  // grid rate, Hz
static const int   RESP_WINDOW      = 128;   // 32 s at 4 Hz, full window
static const int   RESP_MIN_FILL    = 64;    // 16 s of clean signal, first estimate
static const int   RESP_LAG_MIN     = 7;     // 0.57 Hz, about 34 breaths/min
static const int   RESP_LAG_MAX     = 40;    // 0.10 Hz, about 6 breaths/min
static const float RESP_CORR_MIN    = 0.25f; // autocorrelation acceptance
static const float RESP_AGREE_MAX   = 4.0f;  // max spread between estimates
static const uint32_t RESP_MIN_BEATS = 8;    // before trusting RIAV and RIFV
static const int   RESP_SMOOTH      = 3;     // median over this many outputs
static const uint32_t RESP_HOLDOFF_MS = 500; // beat detection pause after a gain step
static const float RESP_FS_BEAT_MIN = 20.0f; // Hz, below this beat-only fusion is refused

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
    bool     preliminary  = false; // true while the window is under 32 s
    float    rate         = NAN;   // fused and smoothed, breaths per minute
    float    riiv         = NAN;
    float    riav         = NAN;
    float    rifv         = NAN;
    float    spread       = NAN;   // standard deviation of contributors
    float    windowSec    = 0.0f;  // clean signal the estimate used
    uint8_t  contributing = 0;
    const char *note      = "";
};

class RespirationMonitor {
public:
    void begin();

    // Full reset. Call when the finger is lost.
    void reset();

    // Feed every RAW IR sample as it arrives, with its millis() timestamp.
    void addSample(uint32_t ir, uint32_t nowMs);

    // Call right after changing LED current. Splices the step instead of
    // discarding the history.
    void markDiscontinuity();

    // Flag motion or an unstable DC level. While on, new grid samples are
    // marked unusable; lookbackMs also marks samples already taken, because
    // the artefact is usually detected after the block containing it.
    void setArtefact(bool on, uint32_t lookbackMs = 0);

    bool     ready() const       { return cleanSamples() >= RESP_MIN_FILL; }
    float    fillSeconds() const { return cleanSamples() / RESP_FS; }
    float    minFillSeconds() const { return RESP_MIN_FILL / RESP_FS; }
    uint32_t beats() const       { return _beatCount; }

    BeatStats beatRate() const;

    RespirationResult compute();

private:
    // --- respiration grids (ring buffers) ---
    float   _riiv[RESP_WINDOW];
    float   _riav[RESP_WINDOW];
    float   _rifv[RESP_WINDOW];
    uint8_t _bad[RESP_WINDOW];
    int   _head   = 0;
    int   _filled = 0;
    int   _beatGrid = 0;          // grid samples since the first beat

    uint32_t _gridStartMs = 0;
    double   _accum       = 0;
    uint32_t _accumN      = 0;
    bool     _badNow      = false;
    bool     _spliceNext  = false;
    float    _riivOffset  = 0.0f;
    float    _riivLast    = NAN;

    // --- beat detection ---
    uint32_t _lastMs   = 0;
    float    _fsEst    = 25.0f;   // measured input sample rate, Hz
    float    _baseline = NAN;
    float    _env      = 0.0f;
    float    _hpIn1 = 0.0f, _hpOut1 = 0.0f, _hpIn2 = 0.0f, _hpOut2 = 0.0f;
    bool     _holdoffPending = false;
    uint32_t _holdoffUntil   = 0;

    float    _acPrev1 = 0.0f, _acPrev2 = 0.0f;
    uint32_t _tPrev1  = 0,    _tPrev2  = 0;

    float    _lastBeatTime  = 0.0f;
    float    _meanInterval  = 0.0f;
    float    _beatAmp       = NAN;   // relative to baseline, per mille
    float    _beatInterval  = NAN;
    uint32_t _beatCount     = 0;

    uint8_t _warmup = 0;   // beats to skip after an artefact restart

    float   _beatTimes[BEAT_RING];
    uint8_t _beatHead   = 0;
    uint8_t _beatFilled = 0;

    // --- output smoothing ---
    float   _hist[RESP_SMOOTH];
    uint8_t _histN = 0, _histHead = 0;

    int   cleanSamples() const;
    void  registerBeat(float timeMs, float amplitude);
    void  pushGrid();
    void  tail(const float *ring, int n, float *out) const;
    float estimate(const float *series, int n) const;
    float smooth(float value);
};