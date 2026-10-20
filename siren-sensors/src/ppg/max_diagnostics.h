/*
 * SIREN - MAX30102 register readback
 *
 * The SparkFun setup() call is a convenience wrapper that maps loose integer
 * arguments onto register bitfields. If the mapping does not land where you
 * expect, the only way to find out is to read the registers back and decode
 * them. Call dumpMaxConfig() once at the end of setup().
 *
 * forceMaxConfig() bypasses the wrapper entirely and writes the bitfields
 * directly, for when you want certainty rather than convenience.
 */

#pragma once

#include <Arduino.h>
#include <Wire.h>

static const uint8_t MAX_I2C_ADDR = 0x57;

static const uint8_t MAXREG_FIFO_CONFIG = 0x08;
static const uint8_t MAXREG_MODE_CONFIG = 0x09;
static const uint8_t MAXREG_SPO2_CONFIG = 0x0A;
static const uint8_t MAXREG_LED1_PA     = 0x0C;
static const uint8_t MAXREG_LED2_PA     = 0x0D;

inline uint8_t maxReadReg(uint8_t reg) {
    Wire.beginTransmission(MAX_I2C_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    Wire.requestFrom((uint8_t)MAX_I2C_ADDR, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
}

inline void maxWriteReg(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(MAX_I2C_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

inline void dumpMaxConfig() {
    uint8_t fifo = maxReadReg(MAXREG_FIFO_CONFIG);
    uint8_t mode = maxReadReg(MAXREG_MODE_CONFIG);
    uint8_t spo2 = maxReadReg(MAXREG_SPO2_CONFIG);

    static const uint16_t avgTable[8]  = {1, 2, 4, 8, 16, 32, 32, 32};
    static const uint16_t srTable[8]   = {50, 100, 200, 400, 800, 1000, 1600, 3200};
    static const uint16_t pwTable[4]   = {69, 118, 215, 411};
    static const uint16_t adcTable[4]  = {2048, 4096, 8192, 16384};

    uint8_t  avgBits = (fifo >> 5) & 0x07;
    uint8_t  srBits  = (spo2 >> 2) & 0x07;
    uint8_t  pwBits  = spo2 & 0x03;
    uint8_t  adcBits = (spo2 >> 5) & 0x03;
    uint8_t  modeBits = mode & 0x07;

    Serial.println(F("--- MAX30102 register readback ---"));
    Serial.printf("  FIFO_CONFIG 0x%02X   averaging = %u\n", fifo, avgTable[avgBits]);
    Serial.printf("  MODE_CONFIG 0x%02X   mode bits = %u", mode, modeBits);
    if (modeBits == 0x02)      Serial.println(F("  (heart rate, red only)"));
    else if (modeBits == 0x03) Serial.println(F("  (SpO2, red + IR)"));
    else if (modeBits == 0x07) Serial.println(F("  (multi-LED)"));
    else                       Serial.println(F("  (UNEXPECTED)"));
    Serial.printf("  SPO2_CONFIG 0x%02X   rate = %u Hz, pulse width = %u us, ADC range = %u nA\n",
                  spo2, srTable[srBits], pwTable[pwBits], adcTable[adcBits]);
    Serial.printf("  LED1 (red) = 0x%02X, LED2 (IR) = 0x%02X\n",
                  maxReadReg(MAXREG_LED1_PA), maxReadReg(MAXREG_LED2_PA));
    Serial.printf("  => expected output rate = %.1f Hz\n",
                  (float)srTable[srBits] / (float)avgTable[avgBits]);
    Serial.println(F("----------------------------------"));
}

/*
 * Direct configuration, bypassing SparkFun's setup().
 *
 *   FIFO_CONFIG 0x4F  averaging 4, rollover enabled, almost-full at 15
 *   MODE_CONFIG 0x03  SpO2 mode, red + IR
 *   SPO2_CONFIG 0x27  ADC range 4096 nA, 100 Hz, 411 us pulse width
 *
 * Expected output rate is 100 / 4 = 25 Hz.
 *
 * For 50 Hz output instead, which gives the respiration beat detector far
 * more to work with, use SPO2_CONFIG 0x2B (200 Hz) with averaging 4. Note
 * that 200 Hz is only legal at 118 us pulse width or shorter in 2-LED mode,
 * so pair it with the 0x29 variant below.
 */
inline void forceMaxConfig(uint8_t ledPower) {
    maxWriteReg(MAXREG_MODE_CONFIG, 0x40);      // reset
    delay(100);

    maxWriteReg(MAXREG_FIFO_CONFIG, 0x4F);
    maxWriteReg(MAXREG_MODE_CONFIG, 0x03);
    maxWriteReg(MAXREG_SPO2_CONFIG, 0x27);      // 100 Hz / avg 4 = 25 Hz out
    // maxWriteReg(MAXREG_SPO2_CONFIG, 0x29);   // 200 Hz, 118 us -> 50 Hz out
    maxWriteReg(MAXREG_LED1_PA, ledPower);
    maxWriteReg(MAXREG_LED2_PA, ledPower);
}