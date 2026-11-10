#pragma once

#include <Arduino.h>

#include <optional>
#include <string>
#include <string_view>

#include "glaze/json/read.hpp"
#include "glaze/json/write.hpp"

namespace Transport {
  struct UsbCommand {
    std::string kind;
    std::optional<uint32_t> request_id;
    std::optional<std::string> data_hex;
  };

  struct HelloMessage {
    std::string_view kind = "hello";
    std::string_view role;
    std::string_view mac;
    std::string_view peer_mac;
    std::string_view key_id;
    uint8_t channel     = 0;
    int power_save      = -1;
    double tx_power_dbm = -1;
    bool ready          = false;
    size_t max_payload  = 0;
  };

  struct ErrorMessage {
    std::string_view kind = "error";
    uint32_t request_id   = 0;
    std::string_view reason;
  };

  struct TxResultMessage {
    std::string_view kind = "tx_result";
    uint32_t request_id   = 0;
    bool mac_delivered    = false;
  };

  struct RxMessage {
    std::string_view kind = "rx";
    std::string_view source_mac;
    std::string_view data_hex;
  };

  struct SleepMessage {
    std::string_view kind = "sleep";
    uint8_t wake_pin      = 0;
    uint32_t heartbeat_ms = 0;
  };

  struct SeatSnapshot {
    uint8_t seat_pin = 0;
    std::string_view occupancy;
    uint32_t uptime_ms = 0;
    std::string_view wake_reason;
  };

  struct UsbReadOptions : glz::opts {
    bool error_on_unknown_keys        = false;
    bool validate_skipped             = true;
    bool validate_trailing_whitespace = true;
  };

  inline void printJson(const auto& value) {
    std::string buffer;
    if (glz::write_json(value, buffer)) {
      Serial.println("{\"kind\":\"error\",\"request_id\":0,\"reason\":\"json_encode_failed\"}");
      return;
    }
    Serial.print(buffer.c_str());
    Serial.print('\n');
  }
}  // namespace Transport
