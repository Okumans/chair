#include "radio_transport.h"

#include <Arduino.h>
#include <WiFi.h>

#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/queue.h"
#include "json_messages.h"
#include "link_config.h"
#include "mbedtls/sha256.h"
#include "protocol_json.h"
#include "transport_codec.h"

namespace Transport {
  static_assert(MAX_PAYLOAD <= ESP_NOW_MAX_DATA_LEN_V2, "Protocol frames require ESP-NOW v2");

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
    Receiver receiver    = nullptr;
    uint64_t sendStarted = 0;

    void hello() {
      uint8_t channel = 0;
      wifi_second_chan_t secondary;
      if (esp_wifi_get_channel(&channel, &secondary) != ESP_OK) channel = 0;
      int8_t txPower = -4;
      if (esp_wifi_get_max_tx_power(&txPower) != ESP_OK) txPower = -4;
      wifi_ps_type_t powerSave;
      const int sleepMode = esp_wifi_get_ps(&powerSave) == ESP_OK ? powerSave : -1;
      char peerMac[18];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(HelloMessage{
          .role         = nodeRole,
          .mac          = WiFi.macAddress().c_str(),
          .peer_mac     = peerMac,
          .key_id       = keyId,
          .channel      = channel,
          .power_save   = sleepMode,
          .tx_power_dbm = txPower / 4.0,
          .ready        = ready,
          .max_payload  = MAX_PAYLOAD,
      });
    }

    void receiveCallback(const esp_now_recv_info_t* info, const uint8_t* data, int size) {
      if (!info) return;
      const uint8_t* mac = info->src_addr;
      if (!mac || !data || memcmp(mac, peer, 6) || size <= 0 || size > MAX_PAYLOAD) return;
      Received packet{};
      packet.size = size;
      memcpy(packet.data, data, size);
      // Reliable application messages retry after transient queue overflow.
      xQueueSend(receivedQueue, &packet, 0);
    }

    void sentCallback(const esp_now_send_info_t*, esp_now_send_status_t status) {
      xQueueSend(sentQueue, &status, 0);
    }

    void error(const char* reason, uint32_t requestId = 0) {
      printJson(ErrorMessage{.request_id = requestId, .reason = reason});
    }

    void handleLine(const char* line) {
      UsbCommand command;
      if (glz::read<UsbReadOptions{}>(command, std::string_view{line})) {
        error("invalid_json");
        return;
      }
      if (command.kind == "hello") {
        hello();
        return;
      }
      uint8_t payload[MAX_PAYLOAD];
      size_t size = 0;
      if ((command.kind == "command" || command.kind == "event_ack") && command.message &&
          command.request_id && *command.request_id) {
        SmartChair::Frame frame;
        if (!SmartChair::fromJson(*command.message, frame) ||
            std::string_view(frame.id.node.data()) != LinkConfig::NODE_ID ||
            (command.kind == "command" && frame.type != SmartChair::Type::CONFIG_SET) ||
            (command.kind == "event_ack" && frame.type != SmartChair::Type::EVENT_ACK)) {
          error("invalid_protocol_command", *command.request_id);
          return;
        }
        const auto bytes = SmartChair::encode(frame);
        send(bytes.data(), bytes.size(), *command.request_id);
        return;
      }
      if (command.kind != "tx" || !command.request_id || *command.request_id == 0 ||
          !command.data_hex || !decodeHex(*command.data_hex, payload, MAX_PAYLOAD, size) || !size) {
        error("invalid_tx");
        return;
      }
      send(payload, size, *command.request_id);
    }
  }  // namespace

  void begin(const char* role, const char* ownMac, const char* peerMac) {
    nodeRole = role;
    Serial.begin(115200);
    // Arduino's asynchronous STA_START handler reapplies this preference to the driver.
    WiFi.setSleep(false);
    if (!WiFi.STA.begin(false)) {
      error("wifi_start_failed");
      hello();
      return;
    }
    WiFi.disconnect();

    uint8_t own[6];
    uint8_t pmk[16];
    uint8_t lmk[16];
    size_t size = 0;
    uint8_t actualMac[6]{};
    if (esp_wifi_get_mac(WIFI_IF_STA, actualMac) != ESP_OK) {
      error("station_mac_failed");
      hello();
      return;
    }
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
    if (mbedtls_sha256(keys, sizeof(keys), digest, 0) != 0) {
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
    sendStarted = esp_timer_get_time() / 1000;
    sending     = true;
    pendingId   = requestId;
    if (esp_now_send(peer, data, size) != ESP_OK) {
      sending = false;
      error("radio_send_failed", requestId);
      return false;
    }
    return true;
  }

  bool busy() { return sending; }

  bool available() { return ready; }

  void setReceiver(Receiver callback) { receiver = callback; }

  void poll() {
    const bool connected = static_cast<bool>(Serial);
    if (connected != wasConnected) {
      reader.reset();
      usbOutput.reset();
      if (connected) hello();
      wasConnected = connected;
    }

    // Bound USB work so continuous input cannot starve radio queue draining.
    for (size_t i = 0; i < 512 && Serial.available(); ++i) {
      const char c = Serial.read();
      if (reader.push(c)) handleLine(reader.line());
    }

    if (connected)
      usbOutput.drain(Serial.availableForWrite(), [](const char* data, size_t size) {
        return Serial.write(reinterpret_cast<const uint8_t*>(data), size);
      });
    if (!ready) return;
    // A missing MAC callback cannot permanently block application delivery.
    if (sending && uint64_t(esp_timer_get_time() / 1000) - sendStarted >= 1000) {
      sending = false;
      error("radio_send_timeout", pendingId);
    }
    esp_now_send_status_t status;
    if (xQueueReceive(sentQueue, &status, 0) == pdTRUE) {
      printJson(TxResultMessage{
          .request_id    = pendingId,
          .mac_delivered = status == ESP_NOW_SEND_SUCCESS,
      });
      sending = false;
    }

    Received packet;
    for (size_t i = 0; i < 8 && xQueueReceive(receivedQueue, &packet, 0) == pdTRUE; ++i) {
      if (receiver) {
        receiver(packet.data, packet.size);
        continue;
      }
      SmartChair::Frame frame;
      if (!SmartChair::decode(std::span(packet.data, packet.size), frame) ||
          std::string_view(frame.id.node.data()) != LinkConfig::NODE_ID ||
          (!SmartChair::telemetry(frame.type) && frame.type != SmartChair::Type::COMMAND_RECEIPT)) {
        error("invalid_radio_frame");
        continue;
      }
      char peerMac[18];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(SmartChair::TelemetryMessage{.source_mac = peerMac,
                                             .message    = SmartChair::toJson(frame)});
    }
  }
}  // namespace Transport
