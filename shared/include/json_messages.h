#pragma once

#include <Arduino.h>

#include <optional>
#include <string>
#include <string_view>

#include "glaze/json/read.hpp"
#include "glaze/json/write.hpp"
#include "protocol_json.h"
#include "usb_output.h"

namespace Transport {
  /// Glaze input envelope for hello, diagnostic bytes and typed protocol controls.
  struct UsbCommand {
    std::string kind;
    std::optional<uint32_t> request_id;
    std::optional<std::string> data_hex;
    std::optional<SmartChair::JsonMessage> message;
  };

  /// USB connection snapshot; ready confirms local initialization, not peer delivery.
  struct HelloMessage {
    std::string_view kind              = UsbKind::HELLO;
    std::optional<uint32_t> request_id = std::nullopt;
    std::string_view role;
    std::string_view mac;
    std::string_view peer_mac;
    std::string_view key_id;
    uint8_t channel     = 0;
    int power_save      = -1;
    double tx_power_dbm = -1;
    bool ready          = false;
    size_t max_payload  = 0;
    bool lifecycle_busy = false;
  };

  /// Structured diagnostic with optional request association; zero is unsolicited.
  struct ErrorMessage {
    std::string_view kind = UsbKind::ERROR;
    uint32_t request_id   = 0;
    std::string_view reason;
    // Only JSON parse failures include diagnostics; never echo arbitrary command contents.
    std::optional<glz::error_code> parse_error = std::nullopt;
    std::optional<size_t> byte_offset          = std::nullopt;
    std::optional<size_t> line_bytes           = std::nullopt;
  };

  /// MAC-layer completion only; reliable protocol events still need EVENT_ACK.
  struct TxResultMessage {
    std::string_view kind = UsbKind::TX_RESULT;
    uint32_t request_id   = 0;
    bool mac_delivered    = false;
  };

  /// Diagnostic binary envelope retained for the bench console.
  struct RxMessage {
    std::string_view kind = UsbKind::RX;
    std::string_view source_mac;
    std::string_view data_hex;
  };

  /// Diagnostic vacant-sleep notice; heartbeat_ms is the legacy bench field.
  struct SleepMessage {
    std::string_view kind = UsbKind::SLEEP;
    uint8_t wake_pin      = 0;
    uint32_t heartbeat_ms = 0;
  };

  /// Legacy bench snapshot schema retained for existing diagnostic tooling.
  struct SeatSnapshot {
    uint8_t seat_pin = 0;
    std::string_view occupancy;
    uint32_t uptime_ms = 0;
    std::string_view wake_reason;
  };

  /// Reject unknown fields, malformed skipped values and trailing non-whitespace.
  struct UsbReadOptions : glz::opts {
    bool error_on_unknown_keys        = true;
    bool validate_skipped             = true;
    bool validate_trailing_whitespace = true;
  };

  inline UsbOutput usbOutput;

  /// Serialize and enqueue one bounded line; queue overflow drops output without blocking sensing.
  inline void printJson(const auto& value) {
    std::string buffer;
    if (!glz::write_json(value, buffer)) usbOutput.push(std::move(buffer));
  }
}  // namespace Transport
