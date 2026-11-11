#pragma once

#include <optional>
#include <string>

#include "protocol.h"
#include "transport_codec.h"

namespace SmartChair {
  /// USB representation of a raw sample; JSON axis values retain signed sensor units.
  struct JsonSample {
    uint16_t offset_ms;
    // Glaze 9.1 signed-short formatting promotes to int on ESP toolchains.
    int32_t ax, ay, az, gx, gy, gz;
  };

  /// Glaze USB schema. Type-specific fields are optional; unavailable values serialize as null.
  struct JsonMessage {
    uint8_t version = 1;
    std::string type, node_id, boot_id;
    uint32_t seq = 0;
    std::optional<uint32_t> uptime_ms;
    std::optional<bool> occupied;
    std::optional<uint16_t> battery_mv;
    std::optional<uint32_t> config_revision;
    std::optional<uint8_t> health_flags;
    std::optional<std::string> command_id, status, reason;
    std::optional<uint32_t> listen_remaining_ms;
    std::optional<std::string> ack_boot_id;
    std::optional<uint32_t> ack_seq, revision, heartbeat_s, movement_batch_s;
    std::optional<bool> movement_enabled;
    std::optional<uint32_t> batch_start_ms, duration_ms;
    std::optional<std::string> end_reason;
    std::optional<std::vector<JsonSample>> samples;
  };

  inline constexpr std::array<std::string_view, 7> STATUS_NAMES{
      "applied", "already_applied", "revision_conflict", "stale_revision",
      "busy",    "received",        "command_conflict"};
  inline constexpr std::array<std::string_view, 3> REASON_NAMES{"seat_occupied", "movement_started",
                                                                "scheduled"};
  inline constexpr std::array<std::string_view, 5> END_NAMES{"interval", "capacity", "stopped",
                                                             "vacant", "disabled"};

  /// Format an unsigned boot identity as exactly 16 lowercase hexadecimal characters.
  inline std::string hex64(uint64_t value) {
    char text[17];
    for (int i = 15; i >= 0; --i) {
      text[i] = "0123456789abcdef"[value & 15];
      value >>= 4;
    }
    return std::string(text, 16);
  }

  /// Read a nonzero 16-character hex identity; the output is unspecified on failure.
  inline bool parse64(std::string_view text, uint64_t& value) {
    if (text.size() != 16) return false;
    value = 0;
    for (char c : text) {
      const auto digit = Transport::hexDigit(c);
      if (!digit) return false;

      value = (value << 4) | *digit;
    }
    return value != 0;
  }

  /// Format the 16 command bytes as a canonical hyphenated UUID string.
  inline std::string uuidString(const Uuid& id) {
    char text[33];
    Transport::encodeHex(id.data(), id.size(), text);
    std::string s(text);
    return s.substr(0, 8) + "-" + s.substr(8, 4) + "-" + s.substr(12, 4) + "-" + s.substr(16, 4) +
           "-" + s.substr(20);
  }

  /// Decode a nonzero hyphenated UUID; the output is unspecified on failure.
  inline bool parseUuid(std::string_view text, Uuid& id) {
    if (text.size() != 36) return false;
    std::string compact;
    for (size_t i = 0; i < text.size(); ++i) {
      if (i == 8 || i == 13 || i == 18 || i == 23) {
        if (text[i] != '-') return false;
      } else
        compact.push_back(text[i]);
    }
    size_t size = 0;
    return Transport::decodeHex(compact, id.data(), id.size(), size) && size == 16 && nonzero(id);
  }

  /// Convert a validated frame to the typed USB schema without modifying raw samples.
  inline JsonMessage toJson(const Frame& f) {
    JsonMessage j;
    j.type    = name(f.type);
    j.node_id = f.id.node.data();
    j.boot_id = hex64(f.id.boot);
    j.seq     = f.id.seq;
    if (telemetry(f.type)) {
      j.uptime_ms = f.common.uptime;
      j.occupied  = f.common.occupied;
      if (f.common.battery != UINT16_MAX) j.battery_mv = f.common.battery;
      j.config_revision = f.common.revision;
      j.health_flags    = f.common.health;
    }
    if (f.type == Type::COMMAND_RESULT || f.type == Type::CONFIG_SET ||
        f.type == Type::COMMAND_RECEIPT)
      j.command_id = uuidString(f.command);
    if (f.type == Type::COMMAND_RESULT || f.type == Type::COMMAND_RECEIPT)
      j.status = STATUS_NAMES[uint8_t(f.status)];
    if (f.type == Type::READY) {
      j.reason = REASON_NAMES[uint8_t(f.reason)];
      if (f.listen != UINT32_MAX) j.listen_remaining_ms = f.listen;
    }
    if (f.type == Type::EVENT_ACK) {
      j.ack_boot_id = hex64(f.ack_boot);
      j.ack_seq     = f.ack_seq;
    }
    if (f.type == Type::CONFIG_SET) {
      j.revision         = f.config.revision;
      j.heartbeat_s      = f.config.heartbeat_s;
      j.movement_batch_s = f.config.movement_batch_s;
      j.movement_enabled = f.config.movement_enabled;
    }
    if (f.type == Type::MOVEMENT_BATCH) {
      j.batch_start_ms = f.start;
      j.duration_ms    = f.duration;
      j.end_reason     = END_NAMES[uint8_t(f.end)];
      j.samples.emplace();
      for (const auto& s : f.samples)
        j.samples->push_back({s.offset, s.ax, s.ay, s.az, s.gx, s.gy, s.gz});
    }
    return j;
  }

  // Only downstream controls are accepted from USB; telemetry always comes from decoded radio
  // frames.
  /// Validate an EVENT_ACK or complete CONFIG_SET from USB; do not use output on failure.
  inline bool fromJson(const JsonMessage& j, Frame& f) {
    if (j.uptime_ms || j.occupied || j.battery_mv || j.config_revision || j.health_flags ||
        j.status || j.reason || j.listen_remaining_ms || j.batch_start_ms || j.duration_ms ||
        j.end_reason || j.samples)
      return false;
    if (j.version != 1 || j.node_id.size() > 15 || j.node_id.empty() ||
        !parse64(j.boot_id, f.id.boot))
      return false;
    f.id.node.fill(0);
    std::copy(j.node_id.begin(), j.node_id.end(), f.id.node.begin());
    f.id.seq = j.seq;
    if (j.type == "EVENT_ACK") {
      if (j.command_id || j.revision || j.heartbeat_s || j.movement_batch_s || j.movement_enabled)
        return false;
      if (!j.ack_boot_id || !j.ack_seq || !parse64(*j.ack_boot_id, f.ack_boot)) return false;
      f.type    = Type::EVENT_ACK;
      f.ack_seq = *j.ack_seq;
    } else if (j.type == "CONFIG_SET") {
      if (j.ack_boot_id || j.ack_seq) return false;
      if (!j.command_id || !j.revision || !j.heartbeat_s || !j.movement_batch_s ||
          !j.movement_enabled || !parseUuid(*j.command_id, f.command))
        return false;
      f.type   = Type::CONFIG_SET;
      f.config = {*j.revision, *j.heartbeat_s, *j.movement_batch_s, *j.movement_enabled};
    } else
      return false;
    return valid(f);
  }

  /// Gateway envelope binding decoded chair telemetry to its provisioned source MAC.
  struct TelemetryMessage {
    std::string_view kind = "telemetry";
    std::string_view source_mac;
    JsonMessage message;
  };
}  // namespace SmartChair
