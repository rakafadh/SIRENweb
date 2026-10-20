#include "siren_link.h"

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const uint8_t BROADCAST_ADDR[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// Shared between the WiFi task (callback) and loop(), so every access is
// inside a spinlock. Without this a packet can be half-copied when loop()
// reads it.
static portMUX_TYPE linkMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool       rxPending = false;
static TempPacket          rxBuffer;
static volatile uint32_t   rxCount = 0;
static volatile uint32_t   rxDropped = 0;
static volatile uint32_t   rxGaps = 0;
static volatile uint32_t   rxLastSeq = 0;
static volatile bool       rxHaveSeq = false;

static uint32_t txSeq = 0;
static uint8_t  txNodeId = NODE_TEMP;

// --------------------------------------------------------------- checksum ---
uint8_t sirenChecksum(const uint8_t *data, size_t length) {
    uint8_t sum = 0;
    for (size_t i = 0; i < length; i++) sum = (uint8_t)(sum + data[i]);
    return sum;
}

bool sirenPacketValid(const TempPacket &pkt) {
    if (pkt.magic != SIREN_MAGIC) return false;
    if (pkt.version != SIREN_VERSION) return false;
    const uint8_t *raw = (const uint8_t *)&pkt;
    return sirenChecksum(raw, sizeof(TempPacket) - 1) == pkt.checksum;
}

// ------------------------------------------------------------------ init ----
bool sirenLinkBegin() {
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    // Lock the channel explicitly. ESP-NOW peers only hear each other on the
    // same channel, and a mismatch fails silently with no error anywhere.
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(SIREN_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

    if (esp_now_init() != ESP_OK) {
        Serial.println(F("ERROR: esp_now_init failed."));
        return false;
    }

    Serial.print(F("ESP-NOW up on channel "));
    Serial.print(SIREN_CHANNEL);
    Serial.print(F(", this board's MAC is "));
    Serial.println(WiFi.macAddress());
    return true;
}

// ---------------------------------------------------------------- sender ----
bool sirenLinkBeginSender() {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, BROADCAST_ADDR, 6);
    peer.channel = SIREN_CHANNEL;
    peer.encrypt = false;

    if (!esp_now_is_peer_exist(BROADCAST_ADDR)) {
        if (esp_now_add_peer(&peer) != ESP_OK) {
            Serial.println(F("ERROR: could not add broadcast peer."));
            return false;
        }
    }
    Serial.println(F("Sender ready (broadcast)."));
    return true;
}

bool sirenSendTemp(TempPacket &pkt) {
    pkt.magic    = SIREN_MAGIC;
    pkt.version  = SIREN_VERSION;
    pkt.nodeId   = txNodeId;
    pkt.type     = PKT_TEMP;
    pkt.seq      = ++txSeq;
    pkt.uptimeMs = millis();

    const uint8_t *raw = (const uint8_t *)&pkt;
    pkt.checksum = sirenChecksum(raw, sizeof(TempPacket) - 1);

    return esp_now_send(BROADCAST_ADDR, raw, sizeof(TempPacket)) == ESP_OK;
}

// -------------------------------------------------------------- receiver ----
static void handleIncoming(const uint8_t *data, int len) {
    if (len != (int)sizeof(TempPacket)) {
        portENTER_CRITICAL(&linkMux);
        rxDropped++;
        portEXIT_CRITICAL(&linkMux);
        return;
    }

    TempPacket pkt;
    memcpy(&pkt, data, sizeof(TempPacket));

    if (!sirenPacketValid(pkt)) {
        portENTER_CRITICAL(&linkMux);
        rxDropped++;
        portEXIT_CRITICAL(&linkMux);
        return;
    }

    portENTER_CRITICAL(&linkMux);
    if (rxHaveSeq && pkt.seq > rxLastSeq + 1) {
        rxGaps += (pkt.seq - rxLastSeq - 1);
    }
    rxLastSeq = pkt.seq;
    rxHaveSeq = true;
    rxBuffer = pkt;
    rxPending = true;
    rxCount++;
    portEXIT_CRITICAL(&linkMux);
}

// The receive callback signature changed in ESP32 Arduino core 3.x. Handling
// both means this file builds on either core without edits.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
static void onDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    (void)info;
    handleIncoming(data, len);
}
#else
static void onDataRecv(const uint8_t *mac, const uint8_t *data, int len) {
    (void)mac;
    handleIncoming(data, len);
}
#endif

bool sirenLinkBeginReceiver() {
    if (esp_now_register_recv_cb(onDataRecv) != ESP_OK) {
        Serial.println(F("ERROR: could not register receive callback."));
        return false;
    }
    Serial.println(F("Receiver ready, listening for temperature packets."));
    return true;
}

bool sirenTakeTemp(TempPacket &out) {
    bool got = false;
    portENTER_CRITICAL(&linkMux);
    if (rxPending) {
        out = rxBuffer;
        rxPending = false;
        got = true;
    }
    portEXIT_CRITICAL(&linkMux);
    return got;
}

uint32_t sirenPacketsReceived() { return rxCount; }
uint32_t sirenPacketsDropped()  { return rxDropped; }
uint32_t sirenSequenceGaps()    { return rxGaps; }