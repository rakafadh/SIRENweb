/*
 * SIREN - PPG node
 * MAX30102 (heart rate, SpO2, respiration) and MPU6050 (vehicle vibration),
 * plus temperature received over ESP-NOW from the separate temperature node.
 *
 * Build with the `ppg` environment and flash to the board that carries the
 * MAX30102 and the MPU6050. The MLX90614 lives on its own board running the
 * `temp` environment.
 *
 * The MPU6050 sits on this board because vibration matters where the finger
 * sensor is: it masks HR and RR, and blocks BP calibration while moving.
 * By default it shares this board's I2C bus with the MAX30102 (0x68 vs
 * 0x57). If it is wired to its own pins, set SIREN_MPU_SDA and
 * SIREN_MPU_SCL in platformio.ini and it runs on the second I2C port.
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
 *   - PPG-derived respiration rate, first estimate after 16 s, 32 s window.
 */

#include <Arduino.h>
#include <Wire.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"
#include "respiration.h"
#include "max_diagnostics.h"
#include "filters.h"
#include "mpu6050.h"
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

// MPU6050. Same bus as the MAX30102 unless its own pins are given.
#ifndef SIREN_MPU_ADDR
#define SIREN_MPU_ADDR 0x68          // 0x69 if AD0 is pulled high
#endif
#if defined(SIREN_MPU_SDA) && defined(SIREN_MPU_SCL)
#define MPU_OWN_BUS 1
#else
#define MPU_OWN_BUS 0
#endif
static const uint32_t IMU_INTERVAL_MS = 20;      // 50 Hz, summarised once per block

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

// Gain lock. Every LED change puts a step in the raw IR, and the BP model
// cannot use a 30 s window that contains one. Motion also moves the DC level,
// which used to make the automatic gain chase it. So once the DC has stayed
// inside the target band for GAIN_LOCK_BLOCKS blocks in a row (1 block = 1 s),
// the LED current is frozen. It is released only when the signal clips for
// GAIN_UNLOCK_SAT_BLOCKS blocks, stays out of the band for
// GAIN_UNLOCK_OUT_BLOCKS blocks (the finger really moved), or the finger is
// lifted. Lock and release are printed as plain lines ("AGC locked ...").
#define GAIN_LOCK 1
static const uint8_t  GAIN_LOCK_BLOCKS       = 3;
static const uint8_t  GAIN_UNLOCK_SAT_BLOCKS = 2;
static const uint8_t  GAIN_UNLOCK_OUT_BLOCKS = 8;

static const uint32_t RESP_COMPUTE_MS = 1000;    // recompute RR once per second
static const float    RESP_BLOCK_DRIFT_MAX = 0.08f; // DC change within one block = artefact

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

// 1 also prints the raw IR waveform for the BP model (siren-bp-model-test,
// scripts/live_bp.py), one line per 25 samples:
//   W,<ms of the block's last sample>,<fs>,<ir_1>,...,<ir_25>
// and a marker whenever the automatic gain changes the LED current, because
// a 30 s window containing that step cannot be used for BP:
//   G,<ms>,<led>
// Both start with a letter, so tools/siren_dashboard.py skips them.
#define WAVEFORM_OUTPUT 1

// --------------------------------------------------------------- globals ----
MAX30105 pox;
RespirationMonitor resp;
PpgFilter redFilter, irFilter;
float fsConfigured = FS_NOMINAL;

uint32_t irBuffer[BUFFER_LENGTH];
uint32_t redBuffer[BUFFER_LENGTH];

int32_t spo2 = 0;       int8_t spo2Valid = 0;
int32_t heartRate = 0;  int8_t hrValid   = 0;

uint8_t ledPower = 0x3C;        // about 12 mA to start
bool    gainChanged = false;
bool    gainLocked  = false;

float fsMeasured = FS_NOMINAL;

// Temperature, as last received over the air
float    tempObject = NAN, tempAmbient = NAN, tempCore = NAN;
float    tempObjectRaw = NAN;

// Motion from this board's MPU6050. A temperature node still running the
// old firmware may also send motion; it is used only if no local IMU answers.
Mpu6050  imu;
bool     imuReady = false;
uint32_t imuLastMs = 0;
float    accelRms = NAN, accelPeak = NAN, gyroRms = NAN;
float    airAccelRms = NAN, airAccelPeak = NAN, airGyroRms = NAN;
uint32_t tempLastRxMs = 0;
bool     tempEverSeen = false;
uint8_t  tempFlags = 0;

int32_t hrHistory[5] = {0, 0, 0, 0, 0};
uint8_t hrCount = 0;

RespirationResult respLatest;
uint32_t respLastMs = 0;
bool respEnabled = false;   // finger present, set by loop()

// --------------------------------------------------------------- helpers ----
// Direct FIFO reader.
//
// The MAX30102 produces 25 samples/s (verified with the FIFO test at boot),
// but reading through SparkFun's check()/available() delivered only about 10:
// the library keeps just 4 samples in its own ring and drops the rest. Here
// the FIFO is read directly: both pointers are read, every waiting sample is
// fetched in one burst, and each sample gets its own timestamp, spaced one
// sample period apart and ending at the read time. Beat timing (HR, RIFV)
// depends on those timestamps being right.
static const uint8_t  MAX_REG_FIFO_WR   = 0x04;
static const uint8_t  MAX_REG_FIFO_RD   = 0x06;
static const uint8_t  MAX_REG_FIFO_DATA = 0x07;
static const float    SAMPLE_PERIOD_MS  = 1000.0f / FS_NOMINAL;

static uint32_t qRed[32], qIr[32], qMs[32];
static uint8_t  qHead = 0, qCount = 0;
static uint32_t lastSampleMs = 0;     // timestamp of the sample just returned
static uint32_t fifoReadErrors = 0;
static double   sampleClockMs  = 0.0;  // running sample clock, ms

#if WAVEFORM_OUTPUT
static uint32_t wfIr[25];
static uint8_t  wfCount = 0;

// Collect raw IR and print one W line per 25 samples. Raw, before any
// filtering: the BP model does its own band-pass and needs the pulse shape.
static void waveformAdd(uint32_t rawIr, uint32_t ms) {
    wfIr[wfCount++] = rawIr;
    if (wfCount < 25) return;
    Serial.printf("W,%lu,%.2f", (unsigned long)ms, fsMeasured);
    for (uint8_t i = 0; i < wfCount; i++) Serial.printf(",%lu", (unsigned long)wfIr[i]);
    Serial.print('\n');
    wfCount = 0;
}
#endif

static void fifoFill() {
    uint8_t wrRaw = maxReadReg(MAX_REG_FIFO_WR);
    uint8_t rdRaw = maxReadReg(MAX_REG_FIFO_RD);
    if (wrRaw == 0xFF || rdRaw == 0xFF) { fifoReadErrors++; return; }   // I2C error
    uint8_t n = (uint8_t)((wrRaw - rdRaw) & 0x1F);
    if (n == 0) return;

    uint32_t now = millis();
    Wire.beginTransmission(MAX_I2C_ADDR);
    Wire.write(MAX_REG_FIFO_DATA);
    if (Wire.endTransmission() != 0) { fifoReadErrors++; return; }   // same as SparkFun: stop, then read

    uint8_t done = 0;
    while (done < n) {
        uint8_t chunk = n - done;
        if (chunk > 20) chunk = 20;                 // 20 x 6 = 120 bytes per request
        uint8_t bytes = chunk * 6;
        if (Wire.requestFrom((uint8_t)MAX_I2C_ADDR, bytes) != bytes) { fifoReadErrors++; return; }
        for (uint8_t k = 0; k < chunk; k++) {
            // One byte per statement: the order of calls inside a single
            // expression is unspecified in C++.
            uint32_t r0 = Wire.read(), r1 = Wire.read(), r2 = Wire.read();
            uint32_t i0 = Wire.read(), i1 = Wire.read(), i2 = Wire.read();
            uint8_t idx = (qHead + qCount) & 0x1F;
            qRed[idx] = ((r0 << 16) | (r1 << 8) | r2) & 0x3FFFF;
            qIr[idx]  = ((i0 << 16) | (i1 << 8) | i2) & 0x3FFFF;

            // Timestamp. The chip samples on its own steady clock, so each
            // sample is one period after the previous one. The read time only
            // nudges that clock (5 % per sample) so it cannot drift, and
            // re-anchors it after a gap of more than three periods.
            uint8_t fromEnd = (uint8_t)(n - 1 - (done + k));
            double anchored = (double)now - fromEnd * (double)SAMPLE_PERIOD_MS;
            double expected = sampleClockMs + SAMPLE_PERIOD_MS;
            if (sampleClockMs <= 0.0 || fabs(anchored - expected) > 3.0 * SAMPLE_PERIOD_MS) {
                sampleClockMs = anchored;
            } else {
                sampleClockMs = expected + 0.05 * (anchored - expected);
            }
            qMs[idx] = (uint32_t)(sampleClockMs + 0.5);
            if (qCount < 32) qCount++; else qHead = (qHead + 1) & 0x1F;
        }
        done += chunk;
    }
}

// One IMU sample every IMU_INTERVAL_MS. Called while waiting for PPG
// samples, so it costs no extra time in the loop.
static void imuPoll() {
    if (!imuReady) return;
    uint32_t now = millis();
    if (now - imuLastMs < IMU_INTERVAL_MS) return;
    imuLastMs = now;
    imu.update();
}

static void readOneSample(uint32_t &red, uint32_t &ir) {
    while (qCount == 0) {
        fifoFill();
        imuPoll();
        if (qCount == 0) delay(2);
    }
    imuPoll();
    uint32_t rawRed = qRed[qHead];
    uint32_t rawIr  = qIr[qHead];
    uint32_t now    = qMs[qHead];
    qHead = (qHead + 1) & 0x1F;
    qCount--;
    lastSampleMs = now;

    // Respiration works on the RAW signal. RIIV lives at 0.1 to 0.6 Hz,
    // which the 0.5 Hz highpass in PpgFilter would remove.
    resp.addSample(rawIr, now);
#if WAVEFORM_OUTPUT
    waveformAdd(rawIr, now);
#endif

    // RR on a fixed one-second cadence, independent of the sample rate. At
    // 10 Hz one loop() block takes 2.5 s, so tying it to loop() was slow.
    if (respEnabled && now - respLastMs >= RESP_COMPUTE_MS) {
        respLastMs = now;
        respLatest = resp.compute();
    }

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
#if GAIN_LOCK
    static uint8_t inBandBlocks = 0, satBlocks = 0, outBlocks = 0;
    if (irDc < FINGER_IR_MIN) {                       // finger lifted: start again on the next finger
        if (gainLocked) Serial.println(F("AGC unlocked (finger lifted)"));
        gainLocked = false;
        inBandBlocks = satBlocks = outBlocks = 0;
        return;
    }
    const bool inBand = irDc >= IR_TARGET_LOW && irDc <= IR_TARGET_HIGH;
    if (gainLocked) {
        satBlocks = (irDc > IR_SATURATED) ? satBlocks + 1 : 0;
        outBlocks = inBand ? 0 : outBlocks + 1;
        if (satBlocks < GAIN_UNLOCK_SAT_BLOCKS && outBlocks < GAIN_UNLOCK_OUT_BLOCKS) return;
        Serial.printf("AGC unlocked (%s), adjusting\n", satBlocks >= GAIN_UNLOCK_SAT_BLOCKS ? "clipping" : "out of range");
        gainLocked = false;
        inBandBlocks = satBlocks = outBlocks = 0;
    } else {
        inBandBlocks = inBand ? inBandBlocks + 1 : 0;
        if (inBandBlocks >= GAIN_LOCK_BLOCKS) {
            gainLocked = true;
            satBlocks = outBlocks = 0;
            Serial.printf("AGC locked at LED %u (IR DC %lu)\n", ledPower, (unsigned long)irDc);
            return;
        }
    }
#else
    if (irDc < FINGER_IR_MIN) return;
#endif

    // Proportional step towards the middle of the target band, so the gain
    // settles in a few blocks instead of many small steps. irDc here is the
    // NEWEST block only, and one block is skipped after every change: the
    // 100-sample buffer still holds samples from before the change, and
    // correcting on those overshoots. The step is limited to x0.5 .. x2.
    static bool holdOneBlock = false;
    if (holdOneBlock) { holdOneBlock = false; return; }
    if (irDc > IR_TARGET_HIGH || irDc < IR_TARGET_LOW) {
        const float target = 0.5f * (IR_TARGET_LOW + IR_TARGET_HIGH);
        float ratio = target / (float)irDc;
        if (ratio < 0.5f) ratio = 0.5f;
        if (ratio > 2.0f) ratio = 2.0f;
        float next = (float)ledPower * ratio;
        if (next < LED_MIN) next = LED_MIN;
        if (next > LED_MAX) next = LED_MAX;
        uint8_t nextLed = (uint8_t)(next + 0.5f);
        if (nextLed != ledPower) {
            setLedPower(nextLed);
            gainChanged = true;
#if WAVEFORM_OUTPUT
            Serial.printf("G,%lu,%u\n", (unsigned long)lastSampleMs, ledPower);
#endif
            holdOneBlock = true;
        }
    }

    // The PPG filters re-acquire the new DC level. Respiration does not
    // restart: the step is spliced out of RIIV and RIAV (see respiration.h).
    if (gainChanged) {
        resp.markDiscontinuity();
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
    int32_t block = (length >= 50) ? 25 : length / 5;
    if (block < 3) block = 3;
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

#if MPU_OWN_BUS
    Wire1.begin(SIREN_MPU_SDA, SIREN_MPU_SCL, I2C_HZ);
    imuReady = imu.begin(&Wire1, SIREN_MPU_ADDR);
    Serial.printf("MPU6050 on its own bus, SDA=GPIO%u SCL=GPIO%u\n", SIREN_MPU_SDA, SIREN_MPU_SCL);
#else
    imuReady = imu.begin(&Wire, SIREN_MPU_ADDR);
#endif
    if (imuReady) {
        Serial.println(F("MPU6050 ready. +/-4 g, +/-500 deg/s, 44 Hz filter."));
    } else {
        Serial.printf("WARNING: MPU6050 did not respond at 0x%02X. Motion data will be unavailable.\n",
                      SIREN_MPU_ADDR);
    }

    // TEMP DIAGNOSTIC: measure the sensor's true output rate. The FIFO is
    // emptied, nothing reads it for one second, then the write pointer shows
    // how many samples the chip produced. Remove once the rate is known.
    for (int pass = 0; pass < 3; pass++) {
        maxWriteReg(0x04, 0);   // FIFO_WR_PTR
        maxWriteReg(0x05, 0);   // OVF_COUNTER
        maxWriteReg(0x06, 0);   // FIFO_RD_PTR
        delay(1000);
        uint8_t produced = maxReadReg(0x04) & 0x1F;
        uint8_t overflow = maxReadReg(0x05) & 0x1F;
        Serial.printf("FIFO TEST %d: sensor produced %u samples in 1 s (overflow %u)\n",
                      pass + 1, produced, overflow);
    }

    resp.begin();
    redFilter.configure(FS_NOMINAL);
    irFilter.configure(FS_NOMINAL);

    // ESP-NOW must come up AFTER the MAX30102 is configured. Bringing up WiFi
    // first leaves the radio calibrating during the I2C setup, which has been
    // known to stretch the first few bus transactions.
    if (!sirenLinkBegin())         while (true) delay(1000);
    if (!sirenLinkBeginReceiver()) while (true) delay(1000);

    Serial.println(F("Rest a fingertip lightly on the sensor and breathe normally."));
    Serial.println(F("Respiration: first estimate after 16 s of steady contact, full 32 s window after that."));
#if CSV_OUTPUT
    Serial.println(F("millis,fs,led,ir_dc,drift,pi,hr,hr_src,sdnn,intervals,spo2,rr,riiv,riav,rifv,spread,beats,tobj,tamb,tcore,accel_rms,accel_peak,gyro_rms"));
#endif

    // Start from an empty FIFO so the first burst is not stale
    maxWriteReg(0x04, 0);
    maxWriteReg(0x05, 0);
    maxWriteReg(0x06, 0);
    qHead = 0; qCount = 0;
    sampleClockMs = 0.0;

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

    // Sample rate from the sample timestamps, not from how long the reads
    // took: with burst FIFO reads, part of a block can come out of the queue
    // instantly.
    static uint32_t prevBlockEndMs = 0;
    for (int32_t i = BUFFER_LENGTH - REFRESH_SAMPLES; i < BUFFER_LENGTH; i++) {
        readOneSample(redBuffer[i], irBuffer[i]);
    }
    if (prevBlockEndMs != 0 && lastSampleMs > prevBlockEndMs) {
        fsMeasured = (float)REFRESH_SAMPLES * 1000.0f / (float)(lastSampleMs - prevBlockEndMs);
    }
    prevBlockEndMs = lastSampleMs;

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

    // Respiration: a lost finger clears the history, motion only masks the
    // affected block. Drift is judged on the newest block rather than the
    // whole buffer, so one movement does not blank several blocks.
    if (!finger) {
        resetRespiration();
    } else {
        float blockDrift = dcDrift(irBuffer + BUFFER_LENGTH - REFRESH_SAMPLES,
                                   REFRESH_SAMPLES);
        bool respArtefact = saturated || blockDrift > RESP_BLOCK_DRIFT_MAX;
        uint32_t blockMs = (uint32_t)(REFRESH_SAMPLES * 1000.0f / fsMeasured);
        resp.setArtefact(respArtefact, respArtefact ? blockMs : 0);
    }
    respEnabled = finger;

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

    // --- respiration is computed once per second inside readOneSample() ---

    // --- temperature, received over the air ---
    TempPacket tpkt;
    while (sirenTakeTemp(tpkt)) {          // drain, keep only the newest
        tempObject    = tpkt.tempObject;
        tempAmbient   = tpkt.tempAmbient;
        tempCore      = tpkt.tempCore;
        tempObjectRaw = tpkt.tempRaw;
        airAccelRms   = tpkt.accelRms;
        airAccelPeak  = tpkt.accelPeak;
        airGyroRms    = tpkt.gyroRms;
        tempFlags     = tpkt.flags;
        tempLastRxMs  = millis();
        tempEverSeen  = true;
    }
    const bool tempFresh = tempEverSeen && (millis() - tempLastRxMs) < TEMP_STALE_MS;
    const bool tempOk    = tempFresh && (tempFlags & TFLAG_SENSOR_OK);

    // --- motion: this block's window from the local IMU ---
    bool imuOk = false;
    if (imuReady) {
        MotionStats m = imu.takeWindow();
        imuOk = m.samples > 0;
        accelRms  = m.accelRms;
        accelPeak = m.accelPeak;
        gyroRms   = m.gyroRms;
    } else if (tempFresh && (tempFlags & TFLAG_IMU_OK)) {
        imuOk = true;                       // old temperature firmware with its own MPU
        accelRms  = airAccelRms;
        accelPeak = airAccelPeak;
        gyroRms   = airGyroRms;
    }

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
        Serial.printf("RR=%4.1f/min(n=%u,sd=%3.1f,%s)  ",
                      respLatest.rate, respLatest.contributing, respLatest.spread,
                      respLatest.preliminary ? "prelim" : "32s");
    } else if (!resp.ready()) {
        Serial.printf("RR=--(%2.0f/%2.0fs,b=%lu)    ", resp.fillSeconds(), resp.minFillSeconds(), resp.beats());
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
    if (fifoReadErrors) Serial.printf("  i2cErr=%lu", (unsigned long)fifoReadErrors);

    if (!finger)            Serial.print(F("  [no finger]"));
    else if (!filtersReady) Serial.print(F("  [filters settling]"));
    else if (unstable)      Serial.print(F("  [dc drifting]"));
    else if (saturated)     Serial.print(F("  [saturated]"));
    else if (weakPulse)     Serial.print(F("  [weak pulse]"));
    else if (gainChanged)   Serial.print(F("  [gain settling]"));
    else if (gainLocked)    Serial.print(F("  [gain locked]"));
    Serial.println();
#endif

    // Gain control on the newest block only (see adjustGain)
    uint64_t blockSum = 0;
    for (int32_t i = BUFFER_LENGTH - REFRESH_SAMPLES; i < BUFFER_LENGTH; i++) blockSum += irBuffer[i];
    adjustGain((uint32_t)(blockSum / REFRESH_SAMPLES));
}