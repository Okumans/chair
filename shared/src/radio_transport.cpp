#include "radio_transport.h"

#include <Arduino.h>
#include <WiFi.h>

#include <atomic>

#include "esp_mac.h"
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
      uint64_t receivedAt;
    };

    QueueHandle_t receivedQueue = nullptr;
    // A timed-out send keeps its callback slot reserved until that callback arrives.
    // A later lifecycle uses another slot, so late completions cannot finish a new send.
    constexpr size_t CALLBACK_SLOTS = 8;
    std::array<std::atomic<unsigned>, CALLBACK_SLOTS> completions{};
    std::atomic<int> callbackSlot{-1};
    uint8_t peer[MAC_BYTES]{};
    uint8_t expectedOwn[MAC_BYTES]{};
    char keyId[KEY_FINGERPRINT_BYTES * 2 + 1]{};
    char stationMac[MAC_TEXT_BYTES]{};
    std::atomic<uint8_t> snapshotChannel{0};
    std::atomic<int> snapshotPower{UNKNOWN_TX_POWER_QUARTERS}, snapshotSleep{-1};
    const char* nodeRole = nullptr;
    std::atomic<bool> ready{false};
    bool setupComplete   = false;
    bool wifiInitialized = false;
    std::atomic<bool> wifiRunning{false};
    std::atomic<bool> sending{false};
    std::atomic<bool> restartRequired{false};
    std::atomic<bool> lifecycleBusy{false};
    std::atomic<bool> rebootRequired{false};
    uint32_t pendingId = 0;
    bool usbAnnounced  = false;
    std::atomic<bool> usbBusReset{false};
    LineReader reader;
    Receiver receiver     = nullptr;
    uint64_t sendStarted  = 0;
    uint64_t helloBarrier = 0;
    bool hasHelloBarrier  = false;

    void usbResetCallback(void*, esp_event_base_t, int32_t, void*) { usbBusReset.store(true); }

    void hello(std::optional<uint32_t> requestId = std::nullopt) {
      // Never enter Wi-Fi from the loop while a stalled lifecycle task may own its locks.
      // These settings were confirmed by the worker; hardware MAC is read before it starts.
      const bool running = wifiRunning.load();
      char peerMac[MAC_TEXT_BYTES];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(HelloMessage{
          .request_id = requestId,
          .role       = nodeRole,
          .mac        = stationMac,
          .peer_mac   = peerMac,
          .key_id     = keyId,
          .channel    = running ? snapshotChannel.load() : uint8_t{0},
          .power_save = running ? snapshotSleep.load() : -1,
          .tx_power_dbm =
              (running ? snapshotPower.load() : UNKNOWN_TX_POWER_QUARTERS) / QUARTERS_PER_DBM,
          .ready          = ready,
          .max_payload    = MAX_PAYLOAD,
          .lifecycle_busy = lifecycleBusy,
      });
    }

    void receiveCallback(const esp_now_recv_info_t* info, const uint8_t* data, int size) {
      if (!info) return;
      const uint8_t* mac = info->src_addr;
      if (!mac || !data || memcmp(mac, peer, MAC_BYTES) || size <= 0 ||
          static_cast<size_t>(size) > MAX_PAYLOAD)
        return;
      Received packet{};
      packet.size       = size;
      packet.receivedAt = uint64_t(esp_timer_get_time());
      memcpy(packet.data, data, size);
      // Reliable application messages retry after transient queue overflow.
      xQueueSend(receivedQueue, &packet, 0);
    }

    template <size_t Slot>
    void sentCallback(const esp_now_send_info_t*, esp_now_send_status_t status) {
      unsigned waiting = 1;
      completions[Slot].compare_exchange_strong(waiting, status == ESP_NOW_SEND_SUCCESS ? 2 : 3);
    }

    using SentCallback = void (*)(const esp_now_send_info_t*, esp_now_send_status_t);
    constexpr std::array<SentCallback, CALLBACK_SLOTS> sentCallbacks{
        sentCallback<0>, sentCallback<1>, sentCallback<2>, sentCallback<3>,
        sentCallback<4>, sentCallback<5>, sentCallback<6>, sentCallback<7>};

    void error(const char* reason, uint32_t requestId = 0) {
      printJson(ErrorMessage{.request_id = requestId, .reason = reason});
    }

    bool stopWifi() {
      const auto result = esp_wifi_stop();
      if (result == ESP_OK || result == ESP_ERR_WIFI_NOT_INIT ||
          result == ESP_ERR_WIFI_NOT_STARTED) {
        wifiRunning = false;
        return true;
      }
      wifiRunning = true;
      return false;
    }

    bool initPeer() {
      int slot = -1;
      for (size_t i = 0; i < CALLBACK_SLOTS; ++i)
        if (completions[i].load() == 0) {
          slot = int(i);
          break;
        }
      if (slot < 0) {
        rebootRequired = true;
        return false;
      }
      uint8_t pmk[KEY_BYTES], lmk[KEY_BYTES];
      size_t size = 0;
      if (!decodeHex(LinkConfig::PMK, pmk, sizeof(pmk), size) || size != KEY_BYTES ||
          !decodeHex(LinkConfig::LMK, lmk, sizeof(lmk), size) || size != KEY_BYTES ||
          esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK ||
          esp_wifi_set_max_tx_power(TX_POWER_QUARTER_DBM) != ESP_OK ||
          esp_wifi_set_channel(LinkConfig::CHANNEL, WIFI_SECOND_CHAN_NONE) != ESP_OK ||
          esp_now_init() != ESP_OK)
        return false;
      esp_now_peer_info_t info{};
      memcpy(info.peer_addr, peer, MAC_BYTES);
      memcpy(info.lmk, lmk, KEY_BYTES);
      info.channel = LinkConfig::CHANNEL;
      info.ifidx   = WIFI_IF_STA;
      info.encrypt = true;
      if (esp_now_set_pmk(pmk) == ESP_OK && esp_now_add_peer(&info) == ESP_OK &&
          esp_now_register_recv_cb(receiveCallback) == ESP_OK &&
          esp_now_register_send_cb(sentCallbacks[slot]) == ESP_OK) {
        callbackSlot    = slot;
        snapshotChannel = LinkConfig::CHANNEL;
        snapshotPower   = TX_POWER_QUARTER_DBM;
        snapshotSleep   = WIFI_PS_NONE;
        return true;
      }
      esp_now_deinit();
      return false;
    }

    void handleLine(const char* line) {
      UsbCommand command;
      const std::string_view input{line};
      const auto parseError = glz::read<UsbReadOptions{}>(command, input);
      if (parseError) {
        printJson(ErrorMessage{
            .reason      = ErrorCode::INVALID_JSON,
            .parse_error = parseError.ec,
            .byte_offset = parseError.count,
            .line_bytes  = input.size(),
        });
        return;
      }
      if (command.kind == UsbKind::HELLO) {
        if (command.request_id) {
          helloBarrier    = uint64_t(esp_timer_get_time());
          hasHelloBarrier = true;
        }
        hello(command.request_id);
        return;
      }
      uint8_t payload[MAX_PAYLOAD];
      size_t size = 0;
      if ((command.kind == UsbKind::COMMAND || command.kind == UsbKind::EVENT_ACK ||
           command.kind == UsbKind::READY_PROBE) &&
          command.message && command.request_id && *command.request_id) {
        SmartChair::Frame frame;
        if (!SmartChair::fromJson(*command.message, frame) ||
            std::string_view(frame.id.node.data()) != LinkConfig::NODE_ID ||
            (command.kind == UsbKind::COMMAND && frame.type != SmartChair::Type::CONFIG_SET) ||
            (command.kind == UsbKind::EVENT_ACK && frame.type != SmartChair::Type::EVENT_ACK) ||
            (command.kind == UsbKind::READY_PROBE && frame.type != SmartChair::Type::READY_PROBE)) {
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
    nodeRole                 = role;
    const bool rxBufferReady = Serial.setRxBufferSize(USB_RX_BUFFER_BYTES) == USB_RX_BUFFER_BYTES;
    Serial.onEvent(ARDUINO_HW_CDC_BUS_RESET_EVENT, usbResetCallback);
    Serial.begin(BAUD_RATE);
    uint8_t actualMac[MAC_BYTES]{};
    if (esp_read_mac(actualMac, ESP_MAC_WIFI_STA) != ESP_OK) {
      error(ErrorCode::STATION_MAC_FAILED);
      hello();
      return;
    }
    snprintf(stationMac, sizeof(stationMac), "%02x:%02x:%02x:%02x:%02x:%02x", actualMac[0],
             actualMac[1], actualMac[2], actualMac[3], actualMac[4], actualMac[5]);
    if (!rxBufferReady) {
      error(ErrorCode::USB_RX_BUFFER_FAILED);
      hello();
      return;
    }
    uint8_t pmk[KEY_BYTES];
    uint8_t lmk[KEY_BYTES];
    size_t size = 0;
    if (!decodeMac(ownMac, expectedOwn) || !decodeMac(peerMac, peer) ||
        !memcmp(expectedOwn, peer, MAC_BYTES) ||
        !decodeHex(LinkConfig::PMK, pmk, sizeof(pmk), size) || size != KEY_BYTES ||
        !decodeHex(LinkConfig::LMK, lmk, sizeof(lmk), size) || size != KEY_BYTES) {
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
    if (!receivedQueue) {
      error(ErrorCode::RADIO_INIT_FAILED);
      hello();
      return;
    }
    setupComplete = true;
    hello();
  }

  bool send(const uint8_t* data, size_t size, uint32_t requestId) {
    if (!ready || sending || restartRequired || lifecycleBusy || rebootRequired || !size ||
        size > MAX_PAYLOAD) {
      error(!ready ? ErrorCode::RADIO_NOT_READY
            : sending || restartRequired || lifecycleBusy || rebootRequired
                ? ErrorCode::RADIO_BUSY
                : ErrorCode::INVALID_PAYLOAD,
            requestId);
      return false;
    }
    sendStarted = esp_timer_get_time() / SmartChair::Timing::MICROSECONDS_PER_MILLISECOND;
    sending     = true;
    pendingId   = requestId;
    completions[callbackSlot].store(1);
    if (esp_now_send(peer, data, size) != ESP_OK) {
      sending = false;
      completions[callbackSlot].store(0);
      error(ErrorCode::RADIO_SEND_FAILED, requestId);
      return false;
    }
    return true;
  }

  bool busy() { return sending; }

  bool configured() { return setupComplete; }

  bool available() { return ready; }

  bool stopped() { return !wifiRunning; }

  bool needsRestart() { return restartRequired; }

  bool needsReboot() { return rebootRequired; }

  void setLifecycleBusy(bool value) { lifecycleBusy = value; }

  void setReceiver(Receiver callback) { receiver = callback; }

  bool suspend(bool report) {
    if (!setupComplete || sending) return false;
    if (!wifiRunning) return true;
    if (ready && esp_now_deinit() != ESP_OK) {
      if (report) error(ErrorCode::RADIO_SUSPEND_FAILED);
      return false;
    }
    ready = false;
    if (!stopWifi()) {
      if (wifiInitialized) ready = initPeer();
      if (report) error(ErrorCode::RADIO_SUSPEND_FAILED);
      return false;
    }
    restartRequired = false;
    return true;
  }

  bool resume(bool report) {
    if (rebootRequired) return false;
    if (ready) return true;
    if (!setupComplete) return false;
    if (!wifiInitialized) {
      WiFi.setSleep(false);
      // begin() may enable Wi-Fi even if its wait for the start event fails.
      wifiRunning = true;
      if (!WiFi.STA.begin(false)) {
        stopWifi();
        if (report) error(ErrorCode::WIFI_START_FAILED);
        return false;
      }
      WiFi.disconnect();
      uint8_t actualMac[MAC_BYTES]{};
      if (esp_wifi_get_mac(WIFI_IF_STA, actualMac) != ESP_OK ||
          memcmp(expectedOwn, actualMac, MAC_BYTES)) {
        stopWifi();
        if (report) error(ErrorCode::STATION_MAC_FAILED);
        return false;
      }
      wifiInitialized = true;
    } else if (!wifiRunning) {
      wifiRunning = true;
      if (esp_wifi_start() != ESP_OK) {
        stopWifi();
        if (report) error(ErrorCode::RADIO_RESUME_FAILED);
        return false;
      }
    }
    if (!initPeer()) {
      stopWifi();
      if (report) error(ErrorCode::RADIO_RESUME_FAILED);
      return false;
    }
    ready = true;
    return true;
  }

  void poll() {
    const bool connected = static_cast<bool>(Serial);
    // HWCDC's connection boolean can flap during a healthy link; only a bus reset loses framing.
    if (usbBusReset.exchange(false)) {
      reader.reset();
      usbOutput.reset();
      usbAnnounced = false;
    }
    if (connected && !usbAnnounced) {
      hello();
      usbAnnounced = true;
    }

    // Bound USB work so continuous input cannot starve radio queue draining.
    for (size_t i = 0; i < USB_POLL_BYTES && Serial.available(); ++i) {
      const int value = Serial.read();
      if (value < 0) break;
      if (reader.push(static_cast<char>(value))) handleLine(reader.line());
    }

    if (connected)
      usbOutput.drain(Serial.availableForWrite(), [](const char* data, size_t size) {
        return Serial.write(reinterpret_cast<const uint8_t*>(data), size);
      });
    for (size_t i = 0; i < CALLBACK_SLOTS; ++i) {
      const auto completion = completions[i].load();
      if (completion < 2) continue;
      completions[i].store(0);
      if (int(i) == callbackSlot && sending) {
        printJson(TxResultMessage{.request_id = pendingId, .mac_delivered = completion == 2});
        sending = false;
      }
    }
    if (!ready) return;
    // A missing MAC callback cannot permanently block application delivery.
    if (sending &&
        uint64_t(esp_timer_get_time() / SmartChair::Timing::MICROSECONDS_PER_MILLISECOND) -
                sendStarted >=
            SEND_TIMEOUT_MS) {
      sending         = false;
      restartRequired = true;
      error(ErrorCode::RADIO_SEND_TIMEOUT, pendingId);
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
      if (frame.type == SmartChair::Type::READY && hasHelloBarrier &&
          packet.receivedAt <= helloBarrier)
        continue;
      char peerMac[MAC_TEXT_BYTES];
      snprintf(peerMac, sizeof(peerMac), "%02x:%02x:%02x:%02x:%02x:%02x", peer[0], peer[1], peer[2],
               peer[3], peer[4], peer[5]);
      printJson(SmartChair::TelemetryMessage{.source_mac = peerMac,
                                             .message    = SmartChair::toJson(frame)});
    }
  }
}  // namespace Transport
