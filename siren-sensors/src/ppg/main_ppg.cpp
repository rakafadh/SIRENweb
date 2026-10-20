/*
 * SIREN - PPG node
 * MAX30102 only (heart rate, SpO2, respiration), plus temperature received
 * over ESP-NOW from the separate temperature node.
 *
 * Build with the `ppg` environment and flash to the board that carries the
 * MAX30102. The MLX90614 now lives on its own board running the `temp`
 * environment.
 *
 * This board is the aggregator, so one serial console still shows every
 * vital. Temperature arriving over the air is treated as stale after
 * TEMP_STALE_MS and reported as unavailable rather than held at its last
 * value: a dead temperature node must be visible, not silently frozen.
 *
 * What this firmware does beyond a plain sensor read:
 *
 *   - Measures the real acquisition rate rather than assuming 25 sps, and
 *     rescales the Maxim heart rate, which hardcodes FS = 25 internally.
 *   - Dumps the MAX30102 configuration registers at boot so a mismatch
 *     between requested and actual sample rate is visible, not inferred.
 *   - Automatic LED gain control to keep the IR DC level inside a band where
 *     the pulsatile component survives.
 *   - Rejects any window where the DC level drifted more than 10 percent
 *     start to end, which is what a finger being placed or lifted looks like.
 *   - Reports perfusion index so signal quality is visible.
 *   - Heart rate from beat timing, with the Maxim value as fallback.
 *   - PPG-derived respiration rate on a separate 60 s window.
 */

#include <Arduino.h>
#include <Wire.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"
#include "respiration.h"
#include "max_diagnostics.h"
#include "filters.h"
#include "siren_link.h"

// ---------------------------------------------------------------- config ----
// I2C pins come from platformio.ini, because the two nodes need not be the
// same chip. GPIO22 does not exist on the ESP32-S3, so the classic ESP32 pin
// pair cannot be hardcoded here.
#ifndef SIREN_SDA
#define SIREN_SDA 21
#endif
#ifndef SIREN_SCL
#define SIREN_SCL 22
#endif

static const uint8_t  PIN_SDA = SIREN_SDA;
static const uint8_t  PIN_SCL = SIREN_SCL;
static const uint32_t I2C_HZ  = 100000UL;

static const int32_t  BUFFER_LENGTH   = 100;
static const int32_t  REFRESH_SAMPLES = 25;
static const float    FS_NOMINAL      = 25.0f;   // what spo2_algorithm assumes

static const uint32_t FINGER_IR_MIN  = 50000;
static const uint32_t IR_TARGET_LOW  = 60000;
static const uint32_t IR_TARGET_HIGH = 180000;
static const uint32_t IR_SATURATED   = 220000;
static const float    DC_DRIFT_MAX   = 0.10f;    // 10 percent across a window
static const float    PI_MIN         = 0.15f;    // below this the pulse is too weak

static const uint8_t  LED_MIN = 0x08;
static const uint8_t  LED_MAX = 0xC0;

static const uint8_t  RESP_EVERY_N_CYCLES = 5;   // recompute every ~5 s

// The calibration model now lives on the temperature node, so recalibrating
// means reflashing only the board that holds the sensor. This node receives
// the already-corrected value.
//
// A temperature older than this is reported as unavailable. At a 1 Hz send
// rate, 5 s tolerates four consecutive lost packets before giving up.
static const uint32_t TEMP_STALE_MS = 5000;

// Set to 1 to bypass SparkFun's setup() and write the registers directly
#define FORCE_REGISTER_CONFIG 0

// 0 prints the human-readable console line.
// 1 prints machine-readable CSV, which is what tools/siren_dashboard.py parses.
#define CSV_OUTPUT 1

// 1 also prints the raw IR waveform, one line per refresh block, for the
// host's blood-pressure estimator (siren_app). Each line starts with 'W', so
// parsers that only accept rows beginning with a digit, such as
// tools/siren_dashboard.py, skip it unchanged.
//
//   W,<millis at end of block>,<measured fs>,<ir_1>,...,<ir_25>
//
// Samples are raw counts, oldest first, not filtered and not inverted. The
// measured rate travels with every block because the host has to resample.
#define WAVEFORM_OUTPUT 1

// --------------------------------------------------------------- globals ----
MAX30105 pox;
RespirationMonitor resp;
PpgFilter redFilter, irFilter;
float fsConfigured = FS_NOMINAL;

uint32_t irBuffer[BUFFER_LENGTH];
uint32_t redBuffer[BUFFER_LENGTH];
uint32_t rawIrBlock[REFRESH_SAMPLES];   // unfiltered IR of the newest block

int32_t spo2 = 0;       int8_t spo2Valid = 0;
int32_t heartRate = 0;  int8_t hrValid   = 0;

uint8_t ledPower = 0x3C;        // about 12 mA to start
bool    gainChanged = false;

float fsMeasured = FS_NOMINAL;

// Temperature and motion, as last received over the air
float    tempObject = NAN, tempAmbient = NAN, tempCore = NAN;
float    tempObjectRaw = NAN;
float    accelRms = NAN, accelPeak = NAN, gyroRms = NAN;
uint32_t tempLastRxMs = 0;
bool     tempEverSeen = false;
uint8_t  tempFlags = 0;

int32_t hrHistory[5] = {0, 0, 0, 0, 0};
uint8_t hrCount = 0;

RespirationResult respLatest;
uint8_t respCycle = 0;

// --------------------------------------------------------------- helpers ----
static void readOneSample(uint32_t &red, uint32_t &ir, uint32_t *rawIrOut = nullptr) {
    while (!pox.available()) {
        pox.check();
    }
    uint32_t rawRed = pox.getRed();
    uint32_t rawIr  = pox.getIR();
    pox.nextSample();
    if (rawIrOut) *rawIrOut = rawIr;

    // Respiration works on the RAW signal. RIIV lives at 0.1 to 0.6 Hz,
    // which the 0.5 Hz highpass in PpgFilter would remove.
    resp.addSample(rawIr, millis());

    // Heart rate and SpO2 work on the conditioned signal, which keeps its
    // DC level so the ratio of ratios still has a denominator.
    float fRed = redFilter.process((float)rawRed);
    float fIr  = irFilter.process((float)rawIr);
    red = (uint32_t)(fRed < 0.0f ? 0.0f : fRed);
    ir  = (uint32_t)(fIr  < 0.0f ? 0.0f : fIr);
}

static void setLedPower(uint8_t value) {
    ledPower = value;
    pox.setPulseAmplitudeRed(ledPower);
    pox.setPulseAmplitudeIR(ledPower);
}

static void resetRespiration() {
    resp.reset();
    respLatest = RespirationResult();
}

static void adjustGain(uint32_t irDc) {
    gainChanged = false;
    if (irDc < FINGER_IR_MIN) return;

    if (irDc > IR_TARGET_HIGH && ledPower > LED_MIN) {
        setLedPower(ledPower - 4 < LED_MIN ? LED_MIN : ledPower - 4);
        gainChanged = true;
    } else if (irDc < IR_TARGET_LOW && ledPower < LED_MAX) {
        setLedPower(ledPower + 4 > LED_MAX ? LED_MAX : ledPower + 4);
        gainChanged = true;
    }

    // A step in LED current is indistinguishable from a respiratory baseline
    // shift, and RIIV is measured from exactly that baseline. It is also a
    // step change in DC, which the PPG filters have to re-acquire.
    if (gainChanged) {
        resetRespiration();
        redFilter.reset();
        irFilter.reset();
    }
}

// Peak-to-peak of the detrended IR signal, a proxy for pulse amplitude
static float acAmplitude(const uint32_t *signal, int32_t length) {
    const int32_t window = 15;
    float lo = 1e12f, hi = -1e12f;
    for (int32_t i = 0; i < length; i++) {
        int32_t a = i - window / 2; if (a < 0) a = 0;
        int32_t b = i + window / 2 + 1; if (b > length) b = length;
        float sum = 0;
        for (int32_t j = a; j < b; j++) sum += (float)signal[j];
        float detrended = (float)signal[i] - (sum / (b - a));
        if (detrended < lo) lo = detrended;
        if (detrended > hi) hi = detrended;
    }
    return hi - lo;
}

// Fractional change in DC level from the start of the window to the end.
// A finger arriving or leaving produces a large ramp that the detrending
// above mistakes for an enormous pulse.
static float dcDrift(const uint32_t *signal, int32_t length) {
    const int32_t block = 25;
    double head = 0, tail = 0;
    for (int32_t i = 0; i < block; i++) head += signal[i];
    for (int32_t i = length - block; i < length; i++) tail += signal[i];
    head /= block;
    tail /= block;
    if (head < 1.0) return 1.0f;
    return (float)(fabs(tail - head) / head);
}

static int32_t medianHr(int32_t newValue) {
    if (hrCount < 5) {
        hrHistory[hrCount++] = newValue;
    } else {
        for (uint8_t i = 0; i < 4; i++) hrHistory[i] = hrHistory[i + 1];
        hrHistory[4] = newValue;
    }
    int32_t sorted[5];
    for (uint8_t i = 0; i < hrCount; i++) sorted[i] = hrHistory[i];
    for (uint8_t i = 1; i < hrCount; i++) {
        int32_t key = sorted[i];
        int8_t j = i - 1;
        while (j >= 0 && sorted[j] > key) { sorted[j + 1] = sorted[j]; j--; }
        sorted[j + 1] = key;
    }
    return sorted[hrCount / 2];
}

// ----------------------------------------------------------------- setup ----
void setup() {
    Serial.begin(115200);
    delay(400);
    Serial.println();
    Serial.println(F("SIREN PPG node"));
    Serial.printf("I2C on SDA=GPIO%u SCL=GPIO%u\n", PIN_SDA, PIN_SCL);

    Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);

    if (!pox.begin(Wire, I2C_SPEED_STANDARD)) {
        Serial.println(F("ERROR: MAX30102 not found on I2C."));
        while (true) delay(1000);
    }

#if FORCE_REGISTER_CONFIG
    forceMaxConfig(ledPower);
#else
    pox.setup(ledPower, 4, 2, 100, 215, 4096);
    setLedPower(ledPower);
#endif
    Serial.println(F("MAX30102 ready."));
    dumpMaxConfig();

    resp.begin();
    redFilter.configure(FS_NOMINAL);
    irFilter.configure(FS_NOMINAL);

    // ESP-NOW must come up AFTER the MAX30102 is configured. Bringing up WiFi
    // first leaves the radio calibrating during the I2C setup, which has been
    // known to stretch the first few bus transactions.
    if (!sirenLinkBegin())         while (true) delay(1000);
    if (!sirenLinkBeginReceiver()) while (true) delay(1000);

    Serial.println(F("Rest a fingertip lightly on the sensor and breathe normally."));
    Serial.println(F("Respiration needs 30 s of uninterrupted contact."));
#if CSV_OUTPUT
    Serial.println(F("millis,fs,led,ir_dc,drift,pi,hr,hr_src,sdnn,intervals,spo2,rr,riiv,riav,rifv,spread,beats,tobj,tamb,tcore,accel_rms,accel_peak,gyro_rms"));
#endif

    for (int32_t i = 0; i < BUFFER_LENGTH; i++) {
        readOneSample(redBuffer[i], irBuffer[i]);
    }
}

// ------------------------------------------------------------------ loop ----
void loop() {
    for (int32_t i = REFRESH_SAMPLES; i < BUFFER_LENGTH; i++) {
        redBuffer[i - REFRESH_SAMPLES] = redBuffer[i];
        irBuffer[i - REFRESH_SAMPLES]  = irBuffer[i];
    }

    uint32_t t0 = micros();
    for (int32_t i = BUFFER_LENGTH - REFRESH_SAMPLES; i < BUFFER_LENGTH; i++) {
        readOneSample(redBuffer[i], irBuffer[i],
                      &rawIrBlock[i - (BUFFER_LENGTH - REFRESH_SAMPLES)]);
    }
    uint32_t elapsedUs = micros() - t0;
    uint32_t blockEndMs = millis();
    if (elapsedUs > 0) {
        fsMeasured = (float)REFRESH_SAMPLES * 1000000.0f / (float)elapsedUs;
    }

    // Filter cutoffs are defined relative to the sample rate, so if the real
    // rate differs from what the filters were designed for, redesign them.
    if (fabsf(fsMeasured - fsConfigured) / fsConfigured > 0.20f) {
        fsConfigured = fsMeasured;
        redFilter.configure(fsConfigured);
        irFilter.configure(fsConfigured);
        Serial.printf("Filters redesigned for %.1f Hz\n", fsConfigured);
    }

    maxim_heart_rate_and_oxygen_saturation(
        irBuffer, BUFFER_LENGTH, redBuffer,
        &spo2, &spo2Valid, &heartRate, &hrValid);

    // --- signal quality ---
    uint64_t sum = 0;
    for (int32_t i = 0; i < BUFFER_LENGTH; i++) sum += irBuffer[i];
    uint32_t irDc = (uint32_t)(sum / BUFFER_LENGTH);
    float drift = dcDrift(irBuffer, BUFFER_LENGTH);

    const bool finger    = irDc > FINGER_IR_MIN;
    const bool saturated = irDc > IR_SATURATED;
    const bool unstable  = drift > DC_DRIFT_MAX;

    // Perfusion index is only meaningful on a stable window
    float perfusion = 0.0f;
    if (finger && !unstable) {
        float acPp = acAmplitude(irBuffer, BUFFER_LENGTH);
        perfusion = (irDc > 0) ? (acPp / (float)irDc) * 100.0f : 0.0f;
    }
    const bool weakPulse = finger && !unstable && perfusion < PI_MIN;

    if (!finger || unstable) resetRespiration();

    // --- heart rate, rescaled to the measured acquisition rate ---
    int32_t hrCorrected = (int32_t)(heartRate * (fsMeasured / FS_NOMINAL) + 0.5f);
    bool filtersReady = irFilter.settled() && redFilter.settled();
    bool windowOk = finger && !saturated && !unstable && !weakPulse
                    && !gainChanged && filtersReady;

    // Beat timing is the preferred source. It measures intervals in
    // milliseconds with sub-sample peak refinement, so it does not suffer the
    // integer-sample quantisation that makes the Maxim value jump between a
    // handful of discrete values at low sample rates.
    BeatStats beat = resp.beatRate();
    bool  hrUsable = false;
    float hrValue = 0.0f;
    char  hrSource = '-';

    if (windowOk && beat.valid) {
        hrValue = beat.bpm;
        hrUsable = true;
        hrSource = 'b';
    } else if (windowOk && hrValid && hrCorrected >= 30 && hrCorrected <= 220) {
        hrValue = (float)medianHr(hrCorrected);
        hrUsable = true;
        hrSource = 'm';
    }

    bool spo2Usable = windowOk && spo2Valid;

    // --- respiration, on its own slower cadence ---
    if (++respCycle >= RESP_EVERY_N_CYCLES) {
        respCycle = 0;
        if (finger && !unstable) respLatest = resp.compute();
    }

    // --- temperature, received over the air ---
    TempPacket tpkt;
    while (sirenTakeTemp(tpkt)) {          // drain, keep only the newest
        tempObject    = tpkt.tempObject;
        tempAmbient   = tpkt.tempAmbient;
        tempCore      = tpkt.tempCore;
        tempObjectRaw = tpkt.tempRaw;
        accelRms      = tpkt.accelRms;
        accelPeak     = tpkt.accelPeak;
        gyroRms       = tpkt.gyroRms;
        tempFlags     = tpkt.flags;
        tempLastRxMs  = millis();
        tempEverSeen  = true;
    }
    const bool tempFresh = tempEverSeen && (millis() - tempLastRxMs) < TEMP_STALE_MS;
    const bool tempOk    = tempFresh && (tempFlags & TFLAG_SENSOR_OK);
    const bool imuOk     = tempFresh && (tempFlags & TFLAG_IMU_OK);

#if CSV_OUTPUT
    Serial.printf("%lu,%.1f,%u,%lu,%.3f,%.2f,%.1f,%c,%.1f,%u,%ld,%.1f,%.1f,%.1f,%.1f,%.1f,%lu,%.2f,%.2f,%.2f,%.4f,%.4f,%.2f\n",
                  millis(), fsMeasured, ledPower, irDc, drift, perfusion,
                  hrUsable ? hrValue : -1.0f, hrSource,
                  beat.valid ? beat.sdnn : -1.0f, beat.intervals,
                  (long)(spo2Usable ? spo2 : -1),
                  respLatest.valid ? respLatest.rate : -1.0f,
                  respLatest.riiv, respLatest.riav, respLatest.rifv,
                  respLatest.spread, resp.beats(),
                  tempOk ? tempObject : NAN,
                  tempOk ? tempAmbient : NAN,
                  tempOk ? tempCore : NAN,
                  imuOk ? accelRms : NAN,
                  imuOk ? accelPeak : NAN,
                  imuOk ? gyroRms : NAN);
#else
    Serial.printf("t=%6.1fs  fs=%4.1fHz  IR=%6lu  PI=%4.2f%%  ",
                  millis() / 1000.0f, fsMeasured, irDc, perfusion);

    if (hrUsable)   Serial.printf("HR=%3.0fbpm(%c)  ", hrValue, hrSource);
    else            Serial.print(F("HR=--         "));

    if (spo2Usable) Serial.printf("SpO2=%3ld%%  ", (long)spo2);
    else            Serial.print(F("SpO2=--    "));

    if (respLatest.valid) {
        Serial.printf("RR=%4.1f/min(n=%u,sd=%3.1f)  ",
                      respLatest.rate, respLatest.contributing, respLatest.spread);
    } else if (!resp.ready()) {
        Serial.printf("RR=--(%2.0f/30s,b=%lu)      ", resp.fillSeconds(), resp.beats());
    } else {
        Serial.printf("RR=--(%s,b=%lu)      ", respLatest.note, resp.beats());
    }

    // Temperature is only shown while it is fresh. A stale value held on
    // screen would look like a working sensor on a node that has died.
    if (tempOk) {
        Serial.printf("Tcore=%.2fC(obj %.2f) Tamb=%.2fC",
                      tempCore, tempObject, tempAmbient);
    } else if (!tempEverSeen) {
        Serial.print(F("Tcore=-- (no temp node)     "));
    } else if (!tempFresh) {
        Serial.printf("Tcore=-- (stale %lus)       ",
                      (millis() - tempLastRxMs) / 1000UL);
    } else {
        Serial.print(F("Tcore=-- (sensor fault)     "));
    }
    if (imuOk) Serial.printf("  accel=%.3fg", accelRms);
    Serial.printf("  rx=%lu/gap=%lu", sirenPacketsReceived(), sirenSequenceGaps());

    if (!finger)            Serial.print(F("  [no finger]"));
    else if (!filtersReady) Serial.print(F("  [filters settling]"));
    else if (unstable)      Serial.print(F("  [dc drifting]"));
    else if (saturated)     Serial.print(F("  [saturated]"));
    else if (weakPulse)     Serial.print(F("  [weak pulse]"));
    else if (gainChanged)   Serial.print(F("  [gain settling]"));
    Serial.println();
#endif

#if WAVEFORM_OUTPUT
    Serial.printf("W,%lu,%.2f", (unsigned long)blockEndMs, fsMeasured);
    for (int32_t i = 0; i < REFRESH_SAMPLES; i++) {
        Serial.printf(",%lu", (unsigned long)rawIrBlock[i]);
    }
    Serial.println();
#endif

    adjustGain(irDc);
}