#include "radio_transport.h"

#include <Arduino.h>
#include <WiFi.h>

#include <cmath>

#include "cJSON.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "freertos/queue.h"
#include "link_config.h"
#include "mbedtls/sha256.h"
#include "transport_codec.h"

namespace Transport {
  namespace {
    struct Received {
      uint8_t data[MAX_PAYLOAD];
      uint16_t size;
    };

    QueueHandle_t receivedQueue = nullptr;
    QueueHandle_t sentQueue     = nullptr;
    uint8_t peer[6]{};
    char keyId[17]{};
    const char* nodeRole = nullptr;
    bool ready           = false;
    bool sending         = false;
    uint32_t pendingId   = 0;
    bool wasConnected    = false;
    LineReader reader;

    void hello() {
      uint8_t channel = 0;
      wifi_second_chan_t secondary;
      if (esp_wifi_get_channel(&channel, &secondary) != ESP_OK) channel = 0;
      int8_t txPower = -4;
      if (esp_wifi_get_max_tx_power(&txPower) != ESP_OK) txPower = -4;
      wifi_ps_type_t powerSave;
      const int sleepMode = esp_wifi_get_ps(&powerSave) == ESP_OK ? powerSave : -1;
      Serial.printf(
          "{\"kind\":\"hello\",\"role\":\"%s\",\"mac\":\"%s\","
          "\"peer_mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\",\"key_id\":\"%s\","
          "\"channel\":%u,\"power_save\":%d,\"tx_power_dbm\":%.2f,\"ready\":%s,\"max_payload\":%u}"
          "\n",
          nodeRole, WiFi.macAddress().c_str(), peer[0], peer[1], peer[2], peer[3], peer[4], peer[5],
          keyId, channel, sleepMode, txPower / 4.0, ready ? "true" : "false",
          static_cast<unsigned>(MAX_PAYLOAD));
    }

    void receiveCallback(const uint8_t* mac, const uint8_t* data, int size) {
      if (!mac || !data || memcmp(mac, peer, 6) || size <= 0 || size > MAX_PAYLOAD) return;
      Received packet{};
      packet.size = size;
      memcpy(packet.data, data, size);
      // shortcut: a full queue drops packets, this bench link has no application delivery
      // guarantee.
      xQueueSend(receivedQueue, &packet, 0);
    }

    void sentCallback(const uint8_t*, esp_now_send_status_t status) {
      xQueueSend(sentQueue, &status, 0);
    }

    void error(const char* reason, uint32_t requestId = 0) {
      Serial.printf("{\"kind\":\"error\",\"request_id\":%lu,\"reason\":\"%s\"}\n",
                    static_cast<unsigned long>(requestId), reason);
    }

    void handleLine(const char* line) {
      // cJSON strings cannot represent embedded NULs; reject their escaped form too.
      cJSON* document =
          strstr(line, "\\u0000") ? nullptr : cJSON_ParseWithOpts(line, nullptr, true);
      if (!document || !cJSON_IsObject(document)) {
        cJSON_Delete(document);
        error("invalid_json");
        return;
      }
      const cJSON* kind = cJSON_GetObjectItemCaseSensitive(document, "kind");
      if (cJSON_IsString(kind) && !strcmp(kind->valuestring, "hello")) {
        hello();
        cJSON_Delete(document);
        return;
      }
      const cJSON* id   = cJSON_GetObjectItemCaseSensitive(document, "request_id");
      const cJSON* data = cJSON_GetObjectItemCaseSensitive(document, "data_hex");
      uint8_t payload[MAX_PAYLOAD];
      size_t size = 0;
      if (!cJSON_IsString(kind) || strcmp(kind->valuestring, "tx") || !cJSON_IsNumber(id) ||
          !std::isfinite(id->valuedouble) || id->valuedouble < 1 || id->valuedouble > UINT32_MAX ||
          floor(id->valuedouble) != id->valuedouble || !cJSON_IsString(data) ||
          !decodeHex(data->valuestring, payload, MAX_PAYLOAD, size) || !size) {
        error("invalid_tx");
      } else {
        send(payload, size, static_cast<uint32_t>(id->valuedouble));
      }
      cJSON_Delete(document);
    }
  }  // namespace

  void begin(const char* role, const char* ownMac, const char* peerMac) {
    nodeRole = role;
    Serial.begin(115200);
    // Arduino's asynchronous STA_START handler reapplies this preference to the driver.
    WiFi.setSleep(false);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    uint8_t own[6];
    uint8_t pmk[16];
    uint8_t lmk[16];
    size_t size = 0;
    uint8_t actualMac[6];
    WiFi.macAddress(actualMac);
    if (!decodeMac(ownMac, own) || !decodeMac(peerMac, peer) || memcmp(own, actualMac, 6) ||
        !memcmp(own, peer, 6) || !decodeHex(LinkConfig::PMK, pmk, sizeof(pmk), size) ||
        size != 16 || !decodeHex(LinkConfig::LMK, lmk, sizeof(lmk), size) || size != 16) {
      error("configure_station_macs_and_keys");
      hello();
      return;
    }

    uint8_t keys[32];
    uint8_t digest[32];
    memcpy(keys, pmk, 16);
    memcpy(keys + 16, lmk, 16);
    if (mbedtls_sha256_ret(keys, sizeof(keys), digest, 0) != 0) {
      error("key_fingerprint_failed");
      hello();
      return;
    }
    encodeHex(digest, 8, keyId);

    receivedQueue = xQueueCreate(8, sizeof(Received));
    sentQueue     = xQueueCreate(1, sizeof(esp_now_send_status_t));
    // Bench experiment: cap transmit power at 8.5 dBm while diagnosing failed packets.
    if (!receivedQueue || !sentQueue || esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK ||
        esp_wifi_set_max_tx_power(34) != ESP_OK ||
        esp_wifi_set_channel(LinkConfig::CHANNEL, WIFI_SECOND_CHAN_NONE) != ESP_OK ||
        esp_now_init() != ESP_OK || esp_now_set_pmk(pmk) != ESP_OK) {
      error("radio_init_failed");
      hello();
      return;
    }

    esp_now_peer_info_t info{};
    memcpy(info.peer_addr, peer, 6);
    memcpy(info.lmk, lmk, 16);
    info.channel = LinkConfig::CHANNEL;
    info.ifidx   = WIFI_IF_STA;
    info.encrypt = true;
    ready        = esp_now_add_peer(&info) == ESP_OK &&
                   esp_now_register_recv_cb(receiveCallback) == ESP_OK &&
                   esp_now_register_send_cb(sentCallback) == ESP_OK;
    if (!ready) error("radio_peer_failed");
    hello();
  }

  bool send(const uint8_t* data, size_t size, uint32_t requestId) {
    if (!ready || sending || !size || size > MAX_PAYLOAD) {
      error(!ready ? "radio_not_ready" : sending ? "radio_busy" : "invalid_payload", requestId);
      return false;
    }
    sending   = true;
    pendingId = requestId;
    if (esp_now_send(peer, data, size) != ESP_OK) {
      sending = false;
      error("radio_send_failed", requestId);
      return false;
    }
    return true;
  }

  bool busy() { return sending; }

  void poll() {
    const bool connected = static_cast<bool>(Serial);
    if (connected != wasConnected) {
      reader.reset();
      if (connected) hello();
      wasConnected = connected;
    }

    // Bound USB work so continuous input cannot starve radio queue draining.
    for (size_t i = 0; i < 512 && Serial.available(); ++i) {
      const char c = Serial.read();
      if (reader.push(c)) handleLine(reader.line());
    }

    if (!ready) return;
    esp_now_send_status_t status;
    if (xQueueReceive(sentQueue, &status, 0) == pdTRUE) {
      Serial.printf("{\"kind\":\"tx_result\",\"request_id\":%lu,\"mac_delivered\":%s}\n",
                    static_cast<unsigned long>(pendingId),
                    status == ESP_NOW_SEND_SUCCESS ? "true" : "false");
      sending = false;
    }

    Received packet;
    while (xQueueReceive(receivedQueue, &packet, 0) == pdTRUE) {
      char hex[MAX_PAYLOAD * 2 + 1];
      encodeHex(packet.data, packet.size, hex);
      Serial.printf(
          "{\"kind\":\"rx\",\"source_mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
          "\"data_hex\":\"%s\"}\n",
          peer[0], peer[1], peer[2], peer[3], peer[4], peer[5], hex);
    }
  }
}  // namespace Transport
