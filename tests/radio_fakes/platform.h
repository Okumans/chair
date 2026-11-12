#pragma once

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <string>
#include <string_view>
#include <vector>

constexpr int ESP_OK                         = 0;
constexpr int ESP_ERR_WIFI_NOT_INIT          = 101;
constexpr int ESP_ERR_WIFI_NOT_STARTED       = 102;
constexpr int pdTRUE                         = 1;
constexpr size_t ESP_NOW_MAX_DATA_LEN_V2     = 1470;
constexpr int ARDUINO_HW_CDC_BUS_RESET_EVENT = 1;
using esp_event_base_t                       = const char*;
using esp_event_handler_t                    = void (*)(void*, esp_event_base_t, int32_t, void*);

namespace LinkConfig {
  inline constexpr char NODE_ID[]     = "chair-01";
  inline constexpr char SENSOR_MAC[]  = "02:00:00:00:00:01";
  inline constexpr char GATEWAY_MAC[] = "02:00:00:00:00:02";
  inline constexpr char PMK[]         = "00000000000000000000000000000000";
  inline constexpr char LMK[]         = "11111111111111111111111111111111";
  inline constexpr uint8_t CHANNEL    = 1;
}  // namespace LinkConfig

struct FakeSerial {
  std::deque<uint8_t> input;
  std::string output;
  size_t rxCapacity = 256, dropped = 0;
  bool connected = true, allocationSucceeds = true, begun = false;
  esp_event_handler_t resetCallback = nullptr;

  size_t setRxBufferSize(size_t size) {
    assert(!begun);
    if (!allocationSucceeds) return 0;
    rxCapacity = size;
    return size;
  }

  void begin(uint32_t) { begun = true; }

  void onEvent(int event, esp_event_handler_t callback) {
    assert(event == ARDUINO_HW_CDC_BUS_RESET_EVENT);
    resetCallback = callback;
  }

  explicit operator bool() const { return connected; }

  int available() const { return input.size(); }

  int read() {
    if (input.empty()) return -1;
    const int value = input.front();
    input.pop_front();
    return value;
  }

  int availableForWrite() const { return 64; }

  size_t write(const uint8_t* data, size_t size) {
    output.append(reinterpret_cast<const char*>(data), size);
    return size;
  }

  void feed(std::string_view bytes) {
    for (uint8_t byte : bytes) {
      if (input.size() < rxCapacity)
        input.push_back(byte);
      else
        ++dropped;
    }
  }

  void resetBus() {
    if (resetCallback) resetCallback(nullptr, nullptr, ARDUINO_HW_CDC_BUS_RESET_EVENT, nullptr);
  }
};

extern FakeSerial Serial;

struct FakeQueue {
  size_t capacity, itemSize;
  std::deque<std::vector<uint8_t>> items;
};

using QueueHandle_t = FakeQueue*;

inline QueueHandle_t xQueueCreate(size_t count, size_t itemSize) {
  return new FakeQueue{count, itemSize, {}};
}

inline int xQueueSend(QueueHandle_t queue, const void* value, int) {
  if (queue->items.size() == queue->capacity) return 0;
  const auto bytes = static_cast<const uint8_t*>(value);
  queue->items.emplace_back(bytes, bytes + queue->itemSize);
  return pdTRUE;
}

inline int xQueueReceive(QueueHandle_t queue, void* value, int) {
  if (queue->items.empty()) return 0;
  std::memcpy(value, queue->items.front().data(), queue->itemSize);
  queue->items.pop_front();
  return pdTRUE;
}

inline int xQueueReset(QueueHandle_t queue) {
  queue->items.clear();
  return pdTRUE;
}

inline int64_t fakeRadioTime = 0;

inline int64_t esp_timer_get_time() { return fakeRadioTime; }

using wifi_second_chan_t            = int;
using wifi_ps_type_t                = int;
constexpr int WIFI_SECOND_CHAN_NONE = 0, WIFI_PS_NONE = 0, WIFI_IF_STA = 0;

namespace FakeHardware {
  inline int startResult = ESP_OK, stopResult = ESP_OK, peerResult = ESP_OK;
  inline bool beginResult = true;
}  // namespace FakeHardware

inline int esp_wifi_get_channel(uint8_t* primary, int* secondary) {
  *primary   = 1;
  *secondary = 0;
  return ESP_OK;
}

inline int esp_wifi_get_max_tx_power(int8_t* power) {
  *power = 34;
  return ESP_OK;
}

inline int esp_wifi_get_ps(int* mode) {
  *mode = WIFI_PS_NONE;
  return ESP_OK;
}

inline int esp_wifi_get_mac(int, uint8_t* mac) {
  const uint8_t address[]{2, 0, 0, 0, 0, 2};
  std::memcpy(mac, address, sizeof(address));
  return ESP_OK;
}

inline int esp_wifi_set_ps(int) { return ESP_OK; }

inline int esp_wifi_set_max_tx_power(int8_t) { return ESP_OK; }

inline int esp_wifi_set_channel(uint8_t, int) { return ESP_OK; }

inline int esp_wifi_start() { return FakeHardware::startResult; }

inline int esp_wifi_stop() { return FakeHardware::stopResult; }

struct FakeWiFi {
  struct Station {
    bool begin(bool) { return FakeHardware::beginResult; }
  } STA;

  void setSleep(bool) {}

  void disconnect() {}

  std::string macAddress() const { return LinkConfig::GATEWAY_MAC; }
};

inline FakeWiFi WiFi;

struct esp_now_recv_info_t {
  const uint8_t* src_addr;
};

struct esp_now_send_info_t {};

enum esp_now_send_status_t { ESP_NOW_SEND_SUCCESS, ESP_NOW_SEND_FAIL };

struct esp_now_peer_info_t {
  uint8_t peer_addr[6], lmk[16], channel;
  int ifidx;
  bool encrypt;
};

namespace FakeRadio {
  inline void (*sentCallback)(const esp_now_send_info_t*, esp_now_send_status_t) = nullptr;
  inline void (*recvCallback)(const esp_now_recv_info_t*, const uint8_t*, int)   = nullptr;
  inline std::vector<std::vector<uint8_t>> sent;
  inline bool autoComplete = true;
}  // namespace FakeRadio

inline int esp_now_init() { return FakeHardware::peerResult; }

inline int esp_now_deinit() { return ESP_OK; }

inline int esp_now_set_pmk(const uint8_t*) { return ESP_OK; }

inline int esp_now_add_peer(const esp_now_peer_info_t*) { return ESP_OK; }

inline int esp_now_register_recv_cb(void (*callback)(const esp_now_recv_info_t*, const uint8_t*,
                                                     int)) {
  FakeRadio::recvCallback = callback;
  return ESP_OK;
}

inline int esp_now_register_send_cb(void (*callback)(const esp_now_send_info_t*,
                                                     esp_now_send_status_t)) {
  FakeRadio::sentCallback = callback;
  return ESP_OK;
}

inline int esp_now_send(const uint8_t*, const uint8_t* data, size_t size) {
  FakeRadio::sent.emplace_back(data, data + size);
  if (FakeRadio::autoComplete) FakeRadio::sentCallback(nullptr, ESP_NOW_SEND_SUCCESS);
  return ESP_OK;
}

inline int mbedtls_sha256(const uint8_t*, size_t, uint8_t* digest, int) {
  std::memset(digest, 0, 32);
  return 0;
}
