/*
 * SIREN - minimal MPU6050 driver
 *
 * Written directly against the register map, same reasoning as mlx90614.h:
 * the sketch owns Wire setup, and no library gets to re-initialise the bus
 * behind our back. The MPU6050 shares the bus with the MLX90614 at 0x68
 * (0x69 if AD0 is pulled high).
 *
 * What this measures, and why
 *
 * The question is not "where is the pod" but "is the vehicle shaking hard
 * enough to corrupt a PPG reading". So orientation is irrelevant and the
 * useful quantity is the AC component of acceleration, meaning everything
 * left once gravity is removed. Gravity is tracked with a slow high-pass
 * (a long-tau exponential average of the raw vector) rather than assumed to
 * be on any particular axis, so the pod can be mounted at any angle.
 *
 * Both an RMS and a peak are reported over each window. RMS describes
 * sustained road vibration; peak catches a single pothole, which is what
 * actually ruins one 4-second PPG window.
 */

#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <math.h>

static const uint8_t MPU_ADDR_DEFAULT = 0x68;

static const uint8_t MPU_REG_SMPLRT_DIV   = 0x19;
static const uint8_t MPU_REG_CONFIG       = 0x1A;
static const uint8_t MPU_REG_GYRO_CONFIG  = 0x1B;
static const uint8_t MPU_REG_ACCEL_CONFIG = 0x1C;
static const uint8_t MPU_REG_ACCEL_XOUT_H = 0x3B;
static const uint8_t MPU_REG_PWR_MGMT_1   = 0x6B;
static const uint8_t MPU_REG_WHO_AM_I     = 0x75;

// Full-scale settings chosen below: accel +/-4 g, gyro +/-500 deg/s.
// Ambulance vibration peaks well above 2 g over a pothole, and clipping the
// peak would hide exactly the event worth detecting.
static const float MPU_ACCEL_LSB_PER_G   = 8192.0f;    // +/-4 g
static const float MPU_GYRO_LSB_PER_DPS  = 65.5f;      // +/-500 deg/s

struct MotionStats {
    float   accelRms  = 0.0f;   // g, gravity removed, RMS over the window
    float   accelPeak = 0.0f;   // g, largest single deviation in the window
    float   gyroRms   = 0.0f;   // deg/s, RMS magnitude over the window
    uint32_t samples  = 0;
};

class Mpu6050 {
public:
    // Does NOT call Wire.begin(). Returns true if the device identifies
    // itself and returns a plausible first reading.
    bool begin(TwoWire *wire = &Wire, uint8_t address = MPU_ADDR_DEFAULT) {
        _wire = wire;
        _addr = address;

        uint8_t who = readReg(MPU_REG_WHO_AM_I);
        // 0x68 is the genuine part. Common clones answer 0x70, 0x72, 0x73 or
        // 0x75; all are register-compatible for what is used here.
        if (who == 0xFF) return false;

        writeReg(MPU_REG_PWR_MGMT_1, 0x80);     // reset
        delay(100);
        writeReg(MPU_REG_PWR_MGMT_1, 0x01);     // wake, clock from gyro X
        delay(10);

        // Digital low-pass at about 44 Hz. Road vibration of interest sits
        // well below that, and the filter keeps high-frequency sensor noise
        // out of the RMS.
        writeReg(MPU_REG_CONFIG, 0x03);
        writeReg(MPU_REG_SMPLRT_DIV, 0x04);     // 1 kHz / 5 = 200 Hz internal
        writeReg(MPU_REG_ACCEL_CONFIG, 0x08);   // +/-4 g
        writeReg(MPU_REG_GYRO_CONFIG, 0x08);    // +/-500 deg/s

        float ax, ay, az, gx, gy, gz;
        if (!readRaw(ax, ay, az, gx, gy, gz)) return false;

        float mag = sqrtf(ax * ax + ay * ay + az * az);
        // At rest the vector should be about 1 g. Allow a wide band so a pod
        // being handled during boot still passes.
        return mag > 0.3f && mag < 4.0f;
    }

    // Feed one sample into the current window. Call at 20 to 100 Hz.
    void update() {
        float ax, ay, az, gx, gy, gz;
        if (!readRaw(ax, ay, az, gx, gy, gz)) return;

        // Track gravity as a slow average of the raw vector, so the pod can
        // be mounted at any angle and no axis is privileged.
        if (!_haveGravity) {
            _gx = ax; _gy = ay; _gz = az;
            _haveGravity = true;
        } else {
            const float a = 0.02f;              // about a 1 s time constant
            _gx += a * (ax - _gx);
            _gy += a * (ay - _gy);
            _gz += a * (az - _gz);
        }

        float dx = ax - _gx, dy = ay - _gy, dz = az - _gz;
        float dynamic = sqrtf(dx * dx + dy * dy + dz * dz);
        float rot = sqrtf(gx * gx + gy * gy + gz * gz);

        _accelSq += (double)dynamic * dynamic;
        _gyroSq  += (double)rot * rot;
        if (dynamic > _peak) _peak = dynamic;
        _n++;
    }

    // Close the current window and start a new one.
    MotionStats takeWindow() {
        MotionStats s;
        s.samples = _n;
        if (_n > 0) {
            s.accelRms  = sqrtf((float)(_accelSq / _n));
            s.gyroRms   = sqrtf((float)(_gyroSq / _n));
            s.accelPeak = _peak;
        }
        _accelSq = 0;
        _gyroSq = 0;
        _peak = 0.0f;
        _n = 0;
        return s;
    }

private:
    TwoWire *_wire = &Wire;
    uint8_t  _addr = MPU_ADDR_DEFAULT;

    bool  _haveGravity = false;
    float _gx = 0, _gy = 0, _gz = 0;

    double   _accelSq = 0, _gyroSq = 0;
    float    _peak = 0.0f;
    uint32_t _n = 0;

    uint8_t readReg(uint8_t reg) {
        _wire->beginTransmission(_addr);
        _wire->write(reg);
        if (_wire->endTransmission(false) != 0) return 0xFF;
        if (_wire->requestFrom((uint8_t)_addr, (uint8_t)1) != 1) return 0xFF;
        return _wire->read();
    }

    void writeReg(uint8_t reg, uint8_t value) {
        _wire->beginTransmission(_addr);
        _wire->write(reg);
        _wire->write(value);
        _wire->endTransmission();
    }

    bool readRaw(float &ax, float &ay, float &az,
                 float &gx, float &gy, float &gz) {
        _wire->beginTransmission(_addr);
        _wire->write(MPU_REG_ACCEL_XOUT_H);
        if (_wire->endTransmission(false) != 0) return false;
        if (_wire->requestFrom((uint8_t)_addr, (uint8_t)14) != 14) return false;

        uint8_t b[14];
        for (uint8_t i = 0; i < 14; i++) b[i] = _wire->read();

        int16_t rax = (int16_t)((b[0]  << 8) | b[1]);
        int16_t ray = (int16_t)((b[2]  << 8) | b[3]);
        int16_t raz = (int16_t)((b[4]  << 8) | b[5]);
        // b[6], b[7] are the die temperature, unused here
        int16_t rgx = (int16_t)((b[8]  << 8) | b[9]);
        int16_t rgy = (int16_t)((b[10] << 8) | b[11]);
        int16_t rgz = (int16_t)((b[12] << 8) | b[13]);

        ax = rax / MPU_ACCEL_LSB_PER_G;
        ay = ray / MPU_ACCEL_LSB_PER_G;
        az = raz / MPU_ACCEL_LSB_PER_G;
        gx = rgx / MPU_GYRO_LSB_PER_DPS;
        gy = rgy / MPU_GYRO_LSB_PER_DPS;
        gz = rgz / MPU_GYRO_LSB_PER_DPS;
        return true;
    }
};