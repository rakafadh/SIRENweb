/*
 * SIREN - ESP-NOW link between sensor nodes
 *
 * Preliminary transport for a two-board split: a temperature node sends, a
 * PPG node receives and aggregates. Broadcast is used rather than unicast so
 * neither board needs the other's MAC address, which removes the single most
 * annoying step of ESP-NOW bring-up. Every packet carries a node ID, so the
 * receiver can already tell pods apart, and moving to unicast later is a
 * change of peer address only, not of packet format.
 *
 * Both boards MUST be on the same WiFi channel. SIREN_CHANNEL is set on both
 * at init. If they disagree, nothing is received and nothing reports an error,
 * which is the most common silent failure with ESP-NOW.
 *
 * Packet integrity is a plain additive checksum. ESP-NOW already runs its own
 * CRC at the radio layer, so this is only here to catch a struct layout
 * mismatch between firmwares, which is the realistic failure mode when only
 * one of the two boards has been reflashed.
 */

#pragma once

#include <Arduino.h>

static const uint8_t SIREN_MAGIC    = 0x5A;
// Version 2 added the three motion fields. A mismatched pair of firmwares is
// rejected by the version check rather than silently misreading the struct,
// so BOTH boards must be reflashed together after a version bump.
static const uint8_t SIREN_VERSION  = 2;
static const uint8_t SIREN_CHANNEL  = 1;

// Node identifiers. One per physical board.
static const uint8_t NODE_TEMP = 0x10;
static const uint8_t NODE_PPG  = 0x20;

// Payload types
static const uint8_t PKT_TEMP = 0x01;

// Status flags on a sensor packet
static const uint8_t TFLAG_SENSOR_OK = 0x01;   // MLX responded at boot
static const uint8_t TFLAG_SETTLED   = 0x02;   // temperature filter converged
static const uint8_t TFLAG_IMU_OK    = 0x04;   // MPU6050 responded at boot

#pragma pack(push, 1)
struct TempPacket {
    uint8_t  magic;         // SIREN_MAGIC
    uint8_t  version;       // SIREN_VERSION
    uint8_t  nodeId;        // which pod sent this
    uint8_t  type;          // PKT_TEMP
    uint32_t seq;           // increments per packet, gaps reveal loss
    uint32_t uptimeMs;      // sender's millis(), for clock-free staleness
    float    tempObject;    // filtered skin/object temperature, C
    float    tempAmbient;   // filtered ambient (sensor die), C
    float    tempCore;      // after the calibration model, C
    float    tempRaw;       // unfiltered object reading, C
    float    accelRms;      // g, gravity removed, RMS over the send window
    float    accelPeak;     // g, largest single deviation in the window
    float    gyroRms;       // deg/s, RMS magnitude over the window
    uint8_t  flags;         // TFLAG_*
    uint8_t  checksum;      // additive over all preceding bytes
};
#pragma pack(pop)

// Compile-time guard. If this fires, the two firmwares would disagree on
// layout and silently exchange garbage.
static_assert(sizeof(TempPacket) == 42, "TempPacket layout changed");

uint8_t sirenChecksum(const uint8_t *data, size_t length);
bool    sirenPacketValid(const TempPacket &pkt);

// Initialise WiFi in station mode, lock the channel and start ESP-NOW.
// Returns false if any step fails.
bool sirenLinkBegin();

// Sender side. Registers the broadcast peer. Call after sirenLinkBegin().
bool sirenLinkBeginSender();

// Broadcast one packet. Fills magic, version, type, seq and checksum.
bool sirenSendTemp(TempPacket &pkt);

// Receiver side. Installs the receive callback. Call after sirenLinkBegin().
bool sirenLinkBeginReceiver();

// True if a new packet has arrived since the last call. Copies it out.
bool sirenTakeTemp(TempPacket &out);

// Statistics for the receiver
uint32_t sirenPacketsReceived();
uint32_t sirenPacketsDropped();   // failed magic, version or checksum
uint32_t sirenSequenceGaps();     // packets lost in transit