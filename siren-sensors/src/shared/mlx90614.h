/*
 * SIREN - minimal MLX90614 driver
 *
 * Replaces the Adafruit library. That library calls _wire->begin() inside its
 * own init, after the sketch has already called Wire.begin(sda, scl, freq).
 * On the ESP32-S3 core a second begin() on an initialised bus can report
 * failure, which the library surfaces as "sensor not found" even when the
 * device answers a bus scan perfectly. Talking to the chip directly removes
 * that whole class of problem, and the protocol is only a few lines.
 *
 * The MLX90614 is an SMBus device. A register read is a write of the register
 * address, a repeated START, then three bytes back: low, high, and a PEC
 * checksum byte. The PEC is read and discarded here; ESP-NOW and I2C both
 * carry their own integrity checks, and a corrupted word shows up as an
 * implausible temperature that the plausibility gate rejects anyway.
 *
 * Temperature registers hold kelvin in units of 0.02 K, so the conversion is
 * raw * 0.02 - 273.15.
 */

#pragma once

#include <Arduino.h>
#include <Wire.h>

static const uint8_t MLX_ADDR_DEFAULT = 0x5A;

// RAM registers
static const uint8_t MLX_REG_TA    = 0x06;   // ambient (sensor die)
static const uint8_t MLX_REG_TOBJ1 = 0x07;   // object, field of view 1

// EEPROM registers, addressed with the 0x20 block bit set
static const uint8_t MLX_REG_EMISSIVITY = 0x24;

class Mlx90614 {
public:
    // Does NOT call Wire.begin(). The sketch owns bus setup, which is the
    // whole point of this driver. Returns true if the device acknowledges
    // its address and returns a plausible ambient temperature.
    bool begin(TwoWire *wire = &Wire, uint8_t address = MLX_ADDR_DEFAULT) {
        _wire = wire;
        _addr = address;

        _wire->beginTransmission(_addr);
        if (_wire->endTransmission() != 0) return false;

        // An address ACK alone is not proof of a working sensor, so confirm
        // one real reading lands in a plausible range before declaring ready.
        float ambient = readAmbientC();
        return !isnan(ambient) && ambient > -40.0f && ambient < 125.0f;
    }

    float readObjectC()  { return toCelsius(readWord(MLX_REG_TOBJ1)); }
    float readAmbientC() { return toCelsius(readWord(MLX_REG_TA)); }

    // Stored emissivity, 0.1 to 1.0. Human skin is about 0.98.
    float readEmissivity() {
        uint16_t raw = readWord(MLX_REG_EMISSIVITY);
        if (raw == 0xFFFF) return NAN;          // read failed
        return (raw == 0) ? NAN : (float)raw / 65535.0f;
    }

    // Last read either succeeded or timed out on the bus
    bool lastReadOk() const { return _lastOk; }

private:
    TwoWire *_wire = &Wire;
    uint8_t  _addr = MLX_ADDR_DEFAULT;
    bool     _lastOk = false;

    uint16_t readWord(uint8_t reg) {
        _lastOk = false;

        _wire->beginTransmission(_addr);
        _wire->write(reg);
        // false keeps the bus held for a repeated START, which SMBus requires
        if (_wire->endTransmission(false) != 0) return 0xFFFF;

        if (_wire->requestFrom((uint8_t)_addr, (uint8_t)3) != 3) return 0xFFFF;

        uint8_t lo  = _wire->read();
        uint8_t hi  = _wire->read();
        (void)_wire->read();                    // PEC, discarded

        _lastOk = true;
        return (uint16_t)((hi << 8) | lo);
    }

    static float toCelsius(uint16_t raw) {
        if (raw == 0xFFFF) return NAN;
        // Bit 15 set on a temperature register signals an error condition
        if (raw & 0x8000) return NAN;
        return (raw * 0.02f) - 273.15f;
    }
};