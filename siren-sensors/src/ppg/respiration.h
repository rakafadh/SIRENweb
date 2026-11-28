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
 * Each series is resampled to a uniform grid (8 Hz since v4), detrended, low passed,
 * and its dominant rate found (v3: spectral peak in the breathing band). The fused value is the
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
 * ---------------------------------------------------------------------------
 * v3 changes (28 Nov 2026): Mayer waves
 * ---------------------------------------------------------------------------
 * Recordings at rest (28 Nov, 19 min, MAX30102 at 25 Hz) showed the strongest
 * slow component of RIIV and RIFV at 0.09-0.12 Hz (5.6-7 per min): Mayer
 * waves, the 10 s oscillation of vessel tone and heart rate. Breathing at
 * about 14-16 per min was only the second peak (RIAV showed it first). v2
 * searched down to 6 per min with a 10 s moving-average high pass, so at
 * normal breathing depth it locked onto the Mayer wave (outputs 6.5-9.4 per
 * min, valid 30 % of the time); only deep breathing made breathing win.
 *
 *  1. Spectral peak instead of autocorrelation: Hann-windowed DFT (Goertzel
 *     per bin) after a zero-phase 0.06 Hz high pass, searched in the breathing
 *     band 0.14-0.60 Hz (8.4-36 per min; v4: up to half the heart rate). The peak must be a local maximum
 *     inside the band (not the band edge, where Mayer leakage sits) and stand
 *     RESP_PEAK_MIN times above the band's mean power. A clear peak at half
 *     the frequency wins (fundamental over harmonic).
 *  2. Fusion: any two of RIIV, RIAV, RIFV that agree within 4 per min (the
 *     best-quality agreeing pair, or all three). RIAV + RIFV alone still need
 *     a beat sample rate of at least 20 Hz. RIIV is no longer required: it is
 *     the signal most contaminated by Mayer waves.
 *  3. Slow breathing (below 8.4 per min, NEWS2 3 points) is checked first and
 *     reported only when all three signals put a dominant peak in 0.09-0.14 Hz
 *     and agree within 1.5 per min (deep slow breathing does this; Mayer waves
 *     alone do not move RIAV). Otherwise no value: count manually.
 *
 * ---------------------------------------------------------------------------
 * v4 changes (28 Nov 2026), from the 18:02 recording (7.4 min, RR shown 42 %)
 * ---------------------------------------------------------------------------
 *  1. Smoothing is cleared when no RR has been output for RESP_GAP_RESET_MS.
 *     The median of the last three outputs carried values from before a gap,
 *     so the first RR after it matched none of the signals (11 times). There
 *     was also a bug: a reset cleared the count but not the write position,
 *     so the median still read a stale slot. Both are fixed.
 *  2. Mayer guard. A 32 s window resolves about 0.03 Hz, so a Mayer peak at
 *     0.1 Hz spreads to about 0.16 Hz (9.5 per min), inside the breathing
 *     band: RIIV sat at 8.5-10 per min while RIAV said 15-19. If a peak at
 *     0.07-0.14 Hz is at least as strong as the breathing-band peak and that
 *     peak lies within RESP_GUARD_RES resolution steps of it, the search
 *     moves past it to the next peak.
 *  3. Tracking. When fewer than two signals agree, one signal within
 *     RESP_TRACK_TOL of the last agreed rate (agreed within RESP_TRACK_MS) is
 *     output with note "tracking" and contributing = 1. It never extends
 *     itself: the 30 s count from the last real agreement.
 *  4. No fixed ceiling. The search runs up to half the heart rate (the
 *     sampling limit of the beat-derived signals, as in Zhang et al., BSPC
 *     2025), never below 36 per min and at most 90 per min.
 *     result.rateMax reports the ceiling used.
 *  5. Fast breathing. The grid is now 8 Hz with a 3 Hz anti-alias low pass
 *     (two first-order stages) on the raw signal. On the 4 Hz grid with only
 *     a 250 ms average, pulse harmonics folded into RIIV at high heart rates.
 *     RIAV and RIFV are sampled once per beat, so when breathing is fast
 *     against the heart rate they alias, and both land on the same wrong
 *     value (true 40 per min at HR 100 gave 20, in v3 too). If RIIV has a
 *     strong peak above RESP_FAST_HR_FRAC of the heart rate and a beat
 *     estimate sits on one of its aliases (|HR - R|, |HR - 2R|), that beat
 *     estimate is dropped, and RIIV alone is accepted (note "fast").
 *     Deep fast breathing can make the beat detector miss every other beat:
 *     the heart rate halves, the ceiling drops, and the aliased beat signals
 *     agree on a normal-looking rate (true 50 at HR 120 gave 20). The pulse
 *     is checked in the RIIV spectrum: if it sits at twice the beat rate, no
 *     RR is given (note "fast?": count manually).
 *
 * This class must be fed RAW samples. RIIV lives at 0.1 to 0.6 Hz, which the
 * PPG bandpass in filters.h would remove.
 */

#pragma once

#include <Arduino.h>

static const float RESP_FS          = 8.0f;  // grid rate, Hz (v4: 8 Hz, was 4)
static const int   RESP_WINDOW      = 256;   // 32 s at 8 Hz, full window
static const int   RESP_MIN_FILL    = 128;   // 16 s of clean signal, first estimate
static const float RESP_AA_HZ       = 3.0f;  // anti-alias low pass on the raw signal before the grid
static const float RESP_F_MIN       = 0.14f; // Hz, 8.4 breaths/min, above the Mayer band
static const float RESP_F_MAX_MIN   = 0.60f; // Hz, 36 breaths/min: the search ceiling is never lower
static const float RESP_F_MAX_ABS   = 1.50f; // Hz, 90 breaths/min: grid and filter limit (not a clinical cap)
static const float RESP_F_LOW       = 0.09f; // Hz, 5.4 breaths/min, slow-breathing path only
static const float RESP_F_SPEC      = 0.07f; // Hz, lowest bin computed (Mayer guard looks from here)
static const float RESP_GUARD_RES   = 2.0f;  // Mayer guard width, in units of the DFT resolution 4/n Hz
static const uint32_t RESP_GAP_RESET_MS = 2500;  // no RR for longer than this clears the smoothing
static const uint32_t RESP_TRACK_MS = 30000; // one-signal tracking allowed this long after agreement
static const float RESP_TRACK_TOL   = 2.0f;  // breaths/min from the last agreed rate
static const float RESP_FAST_HR_FRAC = 0.30f; // RIIV above this share of the heart rate: beat signals alias
static const float RESP_FAST_QMIN   = 6.0f;  // RIIV peak quality needed to stand alone when fast
static const float RESP_ALIAS_TOL   = 3.0f;  // breaths/min, beat estimate counted as an alias
static const float RESP_DOUBLE_REL  = 2.0f;  // pulse power at 2x vs 1x the beat rate: beats missed
static const float RESP_F_STEP      = 0.0025f; // Hz, DFT bin spacing (0.15 breaths/min)
static const float RESP_HP_HZ       = 0.06f; // zero-phase high pass before the DFT (drift only)
static const float RESP_PEAK_MIN    = 3.0f;  // peak power / mean band power to accept
static const float RESP_LOW_AGREE   = 1.5f;  // breaths/min, slow-breathing path
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
    float    rateMax      = NAN;   // search ceiling used, breaths/min (half the heart rate)
    uint8_t  contributing = 0;     // 1 = tracking on one signal
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
    float    _aa1 = NAN, _aa2 = NAN;  // anti-alias low pass state (two first-order stages)
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
    uint32_t _lastValidMs = 0;     // last sample time with an RR output
    float    _agreeRate   = NAN;   // last RR that two or more signals agreed on
    uint32_t _agreeMs     = 0;

    int   cleanSamples() const;
    void  registerBeat(float timeMs, float amplitude);
    void  pushGrid();
    void  tail(const float *ring, int n, float *out) const;
    // Breathing-band estimate (NAN if none) between RESP_F_MIN and fMax, and
    // its quality (peak / mean power). lowRate/lowDominant describe the
    // 0.09-0.14 Hz band.
    float estimate(const float *series, int n, float fMax, float *quality = nullptr,
                   float *lowRate = nullptr, bool *lowDominant = nullptr) const;
    // True when the pulse in the last RIIV series given to estimate() is at
    // twice hrHz, i.e. the beat detector is missing every other beat.
    bool pulseDoubled(int n, float hrHz) const;
    float smooth(float value);
};