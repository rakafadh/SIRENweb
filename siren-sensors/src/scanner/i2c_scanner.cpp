/*
 * SIREN - I2C bus scanner (diagnostic only)
 * Build with the `scanner` environment. Expect to see 0x57 (MAX30102)
 * and 0x5A (MLX90614) when both are wired correctly.
 */

#include <Arduino.h>
#include <Wire.h>

// Pins come from platformio.ini. Use env:scanner for a classic ESP32 and
// env:scanner_s3 for an ESP32-S3, whose GPIO22 does not exist.
#ifndef SIREN_SDA
#define SIREN_SDA 21
#endif
#ifndef SIREN_SCL
#define SIREN_SCL 22
#endif

static const uint8_t  PIN_SDA = SIREN_SDA;
static const uint8_t  PIN_SCL = SIREN_SCL;
static const uint32_t I2C_HZ  = 100000UL;

void setup() {
    Serial.begin(115200);
    delay(400);
    Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
    Serial.println();
    Serial.println(F("I2C scanner started."));
    Serial.printf("SDA=GPIO%u  SCL=GPIO%u\n", PIN_SDA, PIN_SCL);
}

void loop() {
    uint8_t found = 0;

    Serial.println(F("Scanning 0x01 to 0x7F ..."));
    for (uint8_t addr = 1; addr < 127; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) {
            Serial.print(F("  device at 0x"));
            if (addr < 16) Serial.print('0');
            Serial.print(addr, HEX);
            if (addr == 0x57) Serial.print(F("  <- MAX30102"));
            if (addr == 0x5A) Serial.print(F("  <- MLX90614"));
            Serial.println();
            found++;
        }
    }

    if (found == 0) Serial.println(F("  nothing found. Check SDA/SCL, GND and power."));
    Serial.println();
    delay(3000);
}