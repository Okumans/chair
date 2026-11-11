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
    uint8_t peer[MAC_BYTES]{};
    char keyId[KEY_FINGERPRINT_BYTES * 2 + 1]{};
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
      int8_t txPower = UNKNOWN_TX_POWER_QUARTERS;
      if (esp_wifi_get_max_tx_power(&txPower) != ESP_OK) txPower = UNKNOWN_TX_POWER_QUARTERS;
      wifi_ps_type_t powerSave;
      const int sleepMode = esp_wifi_get_ps(&powerSave) == ESP_OK ? powerSave : -1;
      char peerMac[MAC_TEXT_BYTES];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(HelloMessage{
          .role         = nodeRole,
          .mac          = WiFi.macAddress().c_str(),
          .peer_mac     = peerMac,
          .key_id       = keyId,
          .channel      = channel,
          .power_save   = sleepMode,
          .tx_power_dbm = txPower / QUARTERS_PER_DBM,
          .ready        = ready,
          .max_payload  = MAX_PAYLOAD,
      });
    }

    void receiveCallback(const esp_now_recv_info_t* info, const uint8_t* data, int size) {
      if (!info) return;
      const uint8_t* mac = info->src_addr;
      if (!mac || !data || memcmp(mac, peer, MAC_BYTES) || size <= 0 || size > MAX_PAYLOAD) return;
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
        error(ErrorCode::INVALID_JSON);
        return;
      }
      if (command.kind == UsbKind::HELLO) {
        hello();
        return;
      }
      uint8_t payload[MAX_PAYLOAD];
      size_t size = 0;
      if ((command.kind == UsbKind::COMMAND || command.kind == UsbKind::EVENT_ACK) &&
          command.message && command.request_id && *command.request_id) {
        SmartChair::Frame frame;
        if (!SmartChair::fromJson(*command.message, frame) ||
            std::string_view(frame.id.node.data()) != LinkConfig::NODE_ID ||
            (command.kind == UsbKind::COMMAND && frame.type != SmartChair::Type::CONFIG_SET) ||
            (command.kind == UsbKind::EVENT_ACK && frame.type != SmartChair::Type::EVENT_ACK)) {
          error(ErrorCode::INVALID_PROTOCOL_COMMAND, *command.request_id);
          return;
        }
        const auto bytes = SmartChair::encode(frame);
        send(bytes.data(), bytes.size(), *command.request_id);
        return;
      }
      if (command.kind != UsbKind::TX || !command.request_id || *command.request_id == 0 ||
          !command.data_hex || !decodeHex(*command.data_hex, payload, MAX_PAYLOAD, size) || !size) {
        error(ErrorCode::INVALID_TX);
        return;
      }
      send(payload, size, *command.request_id);
    }
  }  // namespace

  void begin(const char* role, const char* ownMac, const char* peerMac) {
    nodeRole = role;
    Serial.begin(BAUD_RATE);
    // Arduino's asynchronous STA_START handler reapplies this preference to the driver.
    WiFi.setSleep(false);
    if (!WiFi.STA.begin(false)) {
      error(ErrorCode::WIFI_START_FAILED);
      hello();
      return;
    }
    WiFi.disconnect();

    uint8_t own[MAC_BYTES];
    uint8_t pmk[KEY_BYTES];
    uint8_t lmk[KEY_BYTES];
    size_t size = 0;
    uint8_t actualMac[MAC_BYTES]{};
    if (esp_wifi_get_mac(WIFI_IF_STA, actualMac) != ESP_OK) {
      error(ErrorCode::STATION_MAC_FAILED);
      hello();
      return;
    }
    if (!decodeMac(ownMac, own) || !decodeMac(peerMac, peer) || memcmp(own, actualMac, MAC_BYTES) ||
        !memcmp(own, peer, MAC_BYTES) || !decodeHex(LinkConfig::PMK, pmk, sizeof(pmk), size) ||
        size != KEY_BYTES || !decodeHex(LinkConfig::LMK, lmk, sizeof(lmk), size) ||
        size != KEY_BYTES) {
      error(ErrorCode::CONFIGURE_STATION_MACS_AND_KEYS);
      hello();
      return;
    }

    uint8_t keys[KEY_BYTES * 2];
    uint8_t digest[SHA256_BYTES];
    memcpy(keys, pmk, KEY_BYTES);
    memcpy(keys + KEY_BYTES, lmk, KEY_BYTES);
    if (mbedtls_sha256(keys, sizeof(keys), digest, 0) != 0) {
      error(ErrorCode::KEY_FINGERPRINT_FAILED);
      hello();
      return;
    }
    encodeHex(digest, KEY_FINGERPRINT_BYTES, keyId);

    receivedQueue = xQueueCreate(RADIO_QUEUE_DEPTH, sizeof(Received));
    sentQueue     = xQueueCreate(TX_QUEUE_DEPTH, sizeof(esp_now_send_status_t));
    // Bench experiment: cap transmit power at 8.5 dBm while diagnosing failed packets.
    if (!receivedQueue || !sentQueue || esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK ||
        esp_wifi_set_max_tx_power(TX_POWER_QUARTER_DBM) != ESP_OK ||
        esp_wifi_set_channel(LinkConfig::CHANNEL, WIFI_SECOND_CHAN_NONE) != ESP_OK ||
        esp_now_init() != ESP_OK || esp_now_set_pmk(pmk) != ESP_OK) {
      error(ErrorCode::RADIO_INIT_FAILED);
      hello();
      return;
    }

    esp_now_peer_info_t info{};
    memcpy(info.peer_addr, peer, MAC_BYTES);
    memcpy(info.lmk, lmk, KEY_BYTES);
    info.channel = LinkConfig::CHANNEL;
    info.ifidx   = WIFI_IF_STA;
    info.encrypt = true;
    ready        = esp_now_add_peer(&info) == ESP_OK &&
                   esp_now_register_recv_cb(receiveCallback) == ESP_OK &&
                   esp_now_register_send_cb(sentCallback) == ESP_OK;
    if (!ready) error(ErrorCode::RADIO_PEER_FAILED);
    hello();
  }

  bool send(const uint8_t* data, size_t size, uint32_t requestId) {
    if (!ready || sending || !size || size > MAX_PAYLOAD) {
      error(!ready    ? ErrorCode::RADIO_NOT_READY
            : sending ? ErrorCode::RADIO_BUSY
                      : ErrorCode::INVALID_PAYLOAD,
            requestId);
      return false;
    }
    sendStarted = esp_timer_get_time() / SmartChair::Timing::MICROSECONDS_PER_MILLISECOND;
    sending     = true;
    pendingId   = requestId;
    if (esp_now_send(peer, data, size) != ESP_OK) {
      sending = false;
      error(ErrorCode::RADIO_SEND_FAILED, requestId);
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
    for (size_t i = 0; i < USB_POLL_BYTES && Serial.available(); ++i) {
      const char c = Serial.read();
      if (reader.push(c)) handleLine(reader.line());
    }

    if (connected)
      usbOutput.drain(Serial.availableForWrite(), [](const char* data, size_t size) {
        return Serial.write(reinterpret_cast<const uint8_t*>(data), size);
      });
    if (!ready) return;
    // A missing MAC callback cannot permanently block application delivery.
    if (sending &&
        uint64_t(esp_timer_get_time() / SmartChair::Timing::MICROSECONDS_PER_MILLISECOND) -
                sendStarted >=
            SEND_TIMEOUT_MS) {
      sending = false;
      error(ErrorCode::RADIO_SEND_TIMEOUT, pendingId);
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
    for (size_t i = 0; i < RADIO_QUEUE_DEPTH && xQueueReceive(receivedQueue, &packet, 0) == pdTRUE;
         ++i) {
      if (receiver) {
        receiver(packet.data, packet.size);
        continue;
      }
      SmartChair::Frame frame;
      if (!SmartChair::decode(std::span(packet.data, packet.size), frame) ||
          std::string_view(frame.id.node.data()) != LinkConfig::NODE_ID ||
          (!SmartChair::telemetry(frame.type) && frame.type != SmartChair::Type::COMMAND_RECEIPT)) {
        error(ErrorCode::INVALID_RADIO_FRAME);
        continue;
      }
      char peerMac[MAC_TEXT_BYTES];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(SmartChair::TelemetryMessage{.source_mac = peerMac,
                                             .message    = SmartChair::toJson(frame)});
    }
  }
}  // namespace Transport
