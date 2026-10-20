/*
 * SIREN - temperature node
 * MLX90614 alone on its own ESP32, broadcasting over ESP-NOW.
 *
 * Build with the `temp` environment and flash to the board that carries the
 * MLX90614. It samples faster than the old combined firmware did, because it
 * is no longer sharing a loop with the MAX30102's blocking FIFO reads, and
 * sends a packet at a fixed cadence regardless.
 *
 * Wiring, unchanged from the combined build:
 *   3V3 -> VIN, GND -> GND, GPIO21 -> SDA, GPIO22 -> SCL
 *
 * The calibration model lives here rather than on the PPG node, so that a
 * recalibration only means reflashing the board that actually holds the
 * sensor. The raw and filtered values are sent too, so the receiver can still
 * see what the sensor reported before correction.
 */

#include <Arduino.h>
#include <Wire.h>
#include "mlx90614.h"
#include "mpu6050.h"
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

static const uint32_t SAMPLE_INTERVAL_MS = 200;    // 5 Hz MLX sampling
static const uint32_t IMU_INTERVAL_MS    = 20;     // 50 Hz IMU sampling
static const uint32_t SEND_INTERVAL_MS   = 1000;   // 1 Hz on the air
static const uint32_t PRINT_INTERVAL_MS  = 2000;   // local console only

// 0 prints the human-readable console line every PRINT_INTERVAL_MS.
// 1 prints one machine-readable line per packet, for siren_app on the host,
// which reads this board over its own USB cable:
//
//   T,<millis>,<tobj>,<tamb>,<tcore>,<traw>,<accel_rms>,<accel_peak>,<gyro_rms>,<flags>
//
// Unavailable values print as nan. flags carries the same TFLAG_* bits as
// the ESP-NOW packet, which is still sent so the PPG node keeps working.
#define CSV_OUTPUT 1

// Core temperature offset model, T_core = a*T_obj + b*T_amb + c.
//
// PROVISIONAL. Single-point offset only, anchored 2026-09-26 on a forehead
// reading: sensor 34.5 C against a reference of 35.7 C, at an ambient of
// about 29.6 C.
//
// The gain is left at 1.0 because there is currently no second trustworthy
// reference point to fit it from. That is a known weakness, not a claim that
// the gain is correct: a room-surface reading hints at field-of-view dilution,
// which would mean readings far from 35.7 C are progressively too low. A
// febrile patient could read roughly a degree low, which is enough to cross a
// NEWS2 band boundary.
//
// Do not use this for any accuracy claim. Replace it with a multi-point fit
// against water baths at known temperatures before reporting numbers.
static const float TEMP_A = 1.0000f;
static const float TEMP_B = 0.0000f;
static const float TEMP_C = 1.2000f;

// --------------------------------------------------------------- globals ----
Mlx90614 mlx;
Mpu6050  imu;
TempFilter objFilter, ambFilter;

bool  mlxReady = false;
bool  imuReady = false;
float tempObject = NAN, tempAmbient = NAN, tempCore = NAN, tempRaw = NAN;
MotionStats motion;

uint32_t lastSample = 0, lastImu = 0, lastSend = 0, lastPrint = 0;
uint32_t sentOk = 0, sentFail = 0;

// ----------------------------------------------------------------- setup ----
void setup() {
    Serial.begin(115200);
    delay(400);
    Serial.println();
    Serial.println(F("SIREN temperature node"));
    Serial.printf("I2C on SDA=GPIO%u SCL=GPIO%u\n", PIN_SDA, PIN_SCL);

    Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);

    mlxReady = mlx.begin(&Wire, MLX_ADDR_DEFAULT);
    if (!mlxReady) {
        Serial.println(F("ERROR: MLX90614 did not respond at 0x5A."));
        Serial.println(F("Run env:scanner_s3 to check the bus."));
        Serial.println(F("Continuing anyway so the link can still be tested."));
    } else {
        Serial.print(F("MLX90614 ready. Emissivity = "));
        Serial.println(mlx.readEmissivity(), 3);
        Serial.printf("First reading: Tobj=%.2fC Tamb=%.2fC\n",
                      mlx.readObjectC(), mlx.readAmbientC());
    }

    imuReady = imu.begin(&Wire, MPU_ADDR_DEFAULT);
    if (!imuReady) {
        Serial.println(F("WARNING: MPU6050 did not respond at 0x68."));
        Serial.println(F("Motion data will be unavailable."));
    } else {
        Serial.println(F("MPU6050 ready. +/-4 g, +/-500 deg/s, 44 Hz filter."));
    }

    // 3 s smoothing. The reject gate is wide on purpose, see filters.h.
    objFilter.configure(3.0f, 8.0f);
    ambFilter.configure(3.0f, 8.0f);

    if (!sirenLinkBegin())       while (true) delay(1000);
    if (!sirenLinkBeginSender()) while (true) delay(1000);

    Serial.println(F("Broadcasting temperature at 1 Hz.\n"));
}

// ------------------------------------------------------------------ loop ----
void loop() {
    uint32_t now = millis();

    // --- sample ---
    if (mlxReady && (now - lastSample >= SAMPLE_INTERVAL_MS)) {
        lastSample = now;
        tempRaw = mlx.readObjectC();
        float amb = mlx.readAmbientC();
        // A failed bus read returns NAN, which the filter rejects rather than
        // letting it poison the running estimate.
        tempObject  = objFilter.process(tempRaw, now);
        tempAmbient = ambFilter.process(amb, now);
        tempCore    = TEMP_A * tempObject + TEMP_B * tempAmbient + TEMP_C;
    }

    // --- IMU, sampled far faster than it is sent ---
    // Vibration is a statistic over a window, not an instantaneous value, so
    // the useful thing is many samples summarised once per packet.
    if (imuReady && (now - lastImu >= IMU_INTERVAL_MS)) {
        lastImu = now;
        imu.update();
    }

    // --- transmit ---
    if (now - lastSend >= SEND_INTERVAL_MS) {
        lastSend = now;

        if (imuReady) motion = imu.takeWindow();

        TempPacket pkt = {};
        pkt.tempObject  = tempObject;
        pkt.tempAmbient = tempAmbient;
        pkt.tempCore    = tempCore;
        pkt.tempRaw     = tempRaw;
        pkt.accelRms    = imuReady ? motion.accelRms  : NAN;
        pkt.accelPeak   = imuReady ? motion.accelPeak : NAN;
        pkt.gyroRms     = imuReady ? motion.gyroRms   : NAN;
        pkt.flags       = 0;
        if (mlxReady)            pkt.flags |= TFLAG_SENSOR_OK;
        if (objFilter.settled()) pkt.flags |= TFLAG_SETTLED;
        if (imuReady)            pkt.flags |= TFLAG_IMU_OK;

        if (sirenSendTemp(pkt)) sentOk++;
        else                    sentFail++;

#if CSV_OUTPUT
        Serial.printf("T,%lu,%.2f,%.2f,%.2f,%.2f,%.4f,%.4f,%.2f,%u\n",
                      (unsigned long)now, pkt.tempObject, pkt.tempAmbient,
                      pkt.tempCore, pkt.tempRaw, pkt.accelRms, pkt.accelPeak,
                      pkt.gyroRms, (unsigned)pkt.flags);
#endif
    }

#if !CSV_OUTPUT
    // --- local console ---
    if (now - lastPrint >= PRINT_INTERVAL_MS) {
        lastPrint = now;
        Serial.printf("t=%6.1fs  Tcore=%.2fC(raw %.2f)  Tamb=%.2fC  ",
                      now / 1000.0f, tempCore, tempRaw, tempAmbient);
        if (imuReady) {
            Serial.printf("accel=%.3fg peak=%.3fg gyro=%.1fdps  ",
                          motion.accelRms, motion.accelPeak, motion.gyroRms);
        } else {
            Serial.print(F("motion=--  "));
        }
        Serial.printf("sent=%lu fail=%lu", sentOk, sentFail);
        if (!mlxReady)                 Serial.print(F("  [no temp sensor]"));
        else if (!objFilter.settled()) Serial.print(F("  [settling]"));
        if (!imuReady)                 Serial.print(F("  [no imu]"));
        Serial.println();
    }
#endif

    delay(2);
}