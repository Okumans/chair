#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <span>
#include <string_view>
#include <vector>

namespace SmartChair {
  constexpr uint8_t PROTOCOL_VERSION = 1;
  // Keep the durable generation within SQLite's exact signed INTEGER range.
  constexpr uint64_t MAX_BOOT_GENERATION = INT64_MAX;
  constexpr size_t HEADER_SIZE = 32, MAX_FRAME = 1470, MAX_SAMPLES = 75;
  /// Stable v1 wire codes. Values 1–6 require durable application ACKs.
  enum class Type : uint8_t {
    OCCUPIED = 1,
    VACATED,
    HEARTBEAT,
    STATE_SYNC,
    BATTERY_LOW,
    COMMAND_RESULT,
    MOVEMENT_BATCH,
    READY,
    EVENT_ACK,
    CONFIG_SET,
    COMMAND_RECEIPT
  };
  /// Command outcomes; received confirms receipt only and busy permits a later retry.
  enum class Status : uint8_t {
    applied,
    already_applied,
    revision_conflict,
    stale_revision,
    busy,
    received,
    command_conflict
  };
  /// Why an occupied chair has made its radio available for queued commands.
  enum class Reason : uint8_t { seat_occupied, movement_started, scheduled };
  /// Why a movement batch ended; stopped, vacant and disabled are final boundaries.
  enum class End : uint8_t { interval, capacity, stopped, vacant, disabled };
  /// Nonzero 128-bit command identity, independent of the per-boot event sequence.
  using Uuid = std::array<uint8_t, 16>;

  /// Complete persisted settings. Seconds accept the full nonzero uint32 range.
  struct Configuration {
    uint32_t revision = 0, heartbeat_s = 900, movement_batch_s = 3;
    bool movement_enabled = true;
    /// Compare revision and all settings for idempotent configuration application.
    bool operator==(const Configuration&) const = default;
  };

  /// Convert accepted seconds to milliseconds without uint32 overflow.
  constexpr uint64_t milliseconds(uint32_t seconds) { return uint64_t(seconds) * 1000; }

  /// Reject zero timing intervals; recommendations are not upper validation bounds.
  inline bool valid(const Configuration& c) { return c.heartbeat_s && c.movement_batch_s; }

  /// Event identity. Node bytes are zero padded; boot and sequence must be nonzero.
  struct Header {
    std::array<char, 16> node{};
    uint64_t boot = 0;
    uint32_t seq  = 0;
    /// Match an exact node, boot and sequence identity, including retransmissions.
    bool operator==(const Header&) const = default;
  };

  /// Telemetry snapshot. Battery 65535 means unavailable; uptime wraps at 32 bits.
  struct Common {
    uint32_t uptime   = 0;
    bool occupied     = false;
    uint16_t battery  = UINT16_MAX;
    uint32_t revision = 0;
    uint8_t health    = 2;
  };

  /// Original MPU6050 readings plus acquisition offset in milliseconds from batch start.
  struct Sample {
    uint16_t offset = 0;
    int16_t ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
    /// Compare the offset and all six raw sensor axes without calibration or scaling.
    bool operator==(const Sample&) const = default;
  };

  /// Typed decoded packet. Only members belonging to type are encoded on the wire.
  struct Frame {
    Type type = Type::STATE_SYNC;
    Header id;
    Common common;
    Uuid command{};
    Configuration config;
    Status status            = Status::applied;
    Reason reason            = Reason::seat_occupied;
    uint32_t listen          = UINT32_MAX;
    uint64_t boot_generation = 0;
    uint64_t ack_boot        = 0;
    uint32_t ack_seq         = 0;
    uint32_t start = 0, duration = 0;
    End end = End::interval;
    std::vector<Sample> samples;
  };

  /// Return whether this type must be persisted and ACKed after Pi storage commits.
  inline bool reliable(Type t) { return t >= Type::OCCUPIED && t <= Type::COMMAND_RESULT; }

  /// Return whether this type carries the 12-byte common physical-state snapshot.
  inline bool telemetry(Type t) { return t >= Type::OCCUPIED && t <= Type::READY; }

  /// Reject the all-zero UUID reserved for an absent command identity.
  inline bool nonzero(const Uuid& uuid) {
    for (auto b : uuid)
      if (b) return true;
    return false;
  }

  /// Accept 1–15 ASCII name characters followed by zero padding to 16 bytes.
  inline bool validNode(const std::array<char, 16>& node) {
    bool ended = false;
    if (!node[0] || node[15]) return false;
    for (char c : node) {
      if (!c)
        ended = true;
      else if (ended || !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                          (c >= '0' && c <= '9') || c == '-' || c == '_'))
        return false;
    }
    return true;
  }

  // Validate field meaning after decoding. The decoder separately checks exact byte lengths,
  // boolean encodings, reserved bytes and sensor range codes before constructing the frame.
  /// Check semantic field validity; false means the frame must not be processed.
  inline bool valid(const Frame& f) {
    // Every frame needs a provisioned node name, nonzero identity and a supported message type.
    if (!validNode(f.id.node) || !f.id.boot || !f.id.seq || f.type < Type::OCCUPIED ||
        f.type > Type::COMMAND_RECEIPT)
      return false;

    // Zero denotes an older snapshot without ordering metadata; only positive generations
    // establish current state on the Pi. New firmware never creates an untracked boot.
    if (f.type == Type::STATE_SYNC && f.boot_generation > MAX_BOOT_GENERATION) return false;

    // Contact transitions must agree with the physical occupancy carried in the same message.
    if ((f.type == Type::OCCUPIED && !f.common.occupied) ||
        (f.type == Type::VACATED && f.common.occupied))
      return false;

    // Only IMU-unavailable (bit 0) and battery-unavailable (bit 1) are defined in this version.
    if (telemetry(f.type) && (f.common.health & ~3)) return false;

    // An ACK must identify the exact original event; neither target field may be zero.
    if (f.type == Type::EVENT_ACK) return f.ack_boot && f.ack_seq;

    // Configuration requires a command UUID and valid values for the complete configuration.
    if (f.type == Type::CONFIG_SET) return nonzero(f.command) && valid(f.config);

    // Responses must name a command and use a known status. A receipt can say "received",
    // but a reliable result must describe the outcome rather than just transport receipt.
    if (f.type == Type::COMMAND_RESULT || f.type == Type::COMMAND_RECEIPT) {
      return nonzero(f.command) && f.status <= Status::command_conflict &&
             (f.type != Type::COMMAND_RESULT || f.status != Status::received);
    }

    // Availability uses one of the three defined reasons; UINT32_MAX listen time means no close.
    if (f.type == Type::READY) return f.reason <= Reason::scheduled;

    if (f.type == Type::MOVEMENT_BATCH) {
      // Packets hold at most 75 samples. Empty batches are allowed only as final boundaries
      // (stopped, vacant or disabled), when an earlier flush already sent all the samples.
      if (f.samples.size() > MAX_SAMPLES || f.end > End::disabled ||
          (f.samples.empty() && f.end < End::stopped))
        return false;

      uint16_t previous = 0;
      for (size_t i = 0; i < f.samples.size(); ++i) {
        // Acquisition offsets must increase strictly and fit inside the reported batch duration.
        if ((i && f.samples[i].offset <= previous) || f.samples[i].offset > f.duration)
          return false;

        previous = f.samples[i].offset;
      }
    }

    return true;
  }

  inline constexpr std::array<std::string_view, 11> TYPE_NAMES{
      "OCCUPIED",       "VACATED", "HEARTBEAT", "STATE_SYNC", "BATTERY_LOW",    "COMMAND_RESULT",
      "MOVEMENT_BATCH", "READY",   "EVENT_ACK", "CONFIG_SET", "COMMAND_RECEIPT"};

  /// Return the stable JSON type name. Precondition: type has passed valid().
  inline std::string_view name(Type type) { return TYPE_NAMES[uint8_t(type) - 1]; }

  // Explicit byte codecs also define the persisted frame representation.
  /// Little-endian byte accumulator shared by radio and durable storage codecs.
  struct Writer {
    std::vector<uint8_t> bytes;

    /// Append the low width bytes of v, least significant byte first; width is 1–8.
    void put(uint64_t v, size_t width) {
      for (size_t i = 0; i < width; ++i) bytes.push_back(uint8_t(v >> (i * 8)));
    }
  };

  /// Bounded little-endian cursor. A truncated read latches ok=false without overreading.
  struct Reader {
    std::span<const uint8_t> bytes;
    size_t pos = 0;
    bool ok    = true;

    /// Read width bytes (1–8), or return zero and latch failure if input is truncated.
    uint64_t get(size_t width) {
      if (pos + width > bytes.size()) {
        ok = false;
        return 0;
      }
      uint64_t v = 0;
      for (size_t i = 0; i < width; ++i) v |= uint64_t(bytes[pos++]) << (i * 8);
      return v;
    }
  };

  /// Encode a validated frame with explicit little-endian fields; invalid frames return empty.
  inline std::vector<uint8_t> encode(const Frame& f) {
    if (!valid(f)) return {};
    Writer w;
    w.put(PROTOCOL_VERSION, 1);
    w.put(uint8_t(f.type), 1);
    w.put(0, 2);
    for (auto c : f.id.node) w.put(uint8_t(c), 1);
    w.put(f.id.boot, 8);
    w.put(f.id.seq, 4);
    if (telemetry(f.type)) {
      w.put(f.common.uptime, 4);
      w.put(f.common.occupied, 1);
      w.put(f.common.battery, 2);
      w.put(f.common.revision, 4);
      w.put(f.common.health, 1);
    }
    if (f.type == Type::STATE_SYNC) w.put(f.boot_generation, 8);
    if (f.type == Type::CONFIG_SET || f.type == Type::COMMAND_RESULT ||
        f.type == Type::COMMAND_RECEIPT)
      for (auto b : f.command) w.put(b, 1);
    if (f.type == Type::COMMAND_RESULT || f.type == Type::COMMAND_RECEIPT)
      w.put(uint8_t(f.status), 1);
    if (f.type == Type::CONFIG_SET) {
      w.put(f.config.revision, 4);
      w.put(f.config.heartbeat_s, 4);
      w.put(f.config.movement_batch_s, 4);
      w.put(f.config.movement_enabled, 1);
    } else if (f.type == Type::EVENT_ACK) {
      w.put(f.ack_boot, 8);
      w.put(f.ack_seq, 4);
    } else if (f.type == Type::READY) {
      w.put(uint8_t(f.reason), 1);
      w.put(f.listen, 4);
    } else if (f.type == Type::MOVEMENT_BATCH) {
      w.put(f.start, 4);
      w.put(f.duration, 4);
      w.put(f.samples.size(), 2);
      w.put(0, 1);
      w.put(0, 1);
      w.put(uint8_t(f.end), 1);
      w.put(0, 1);
      for (const auto& s : f.samples) {
        w.put(s.offset, 2);
        for (auto v : {s.ax, s.ay, s.az, s.gx, s.gy, s.gz}) w.put(uint16_t(v), 2);
      }
    }
    const size_t length = w.bytes.size() - HEADER_SIZE;
    w.bytes[2]          = uint8_t(length);
    w.bytes[3]          = uint8_t(length >> 8);
    return w.bytes;
  }

  /// Validate one complete packet. On failure, output remains unchanged.
  inline bool decode(std::span<const uint8_t> bytes, Frame& output) {
    if (bytes.size() < HEADER_SIZE || bytes.size() > MAX_FRAME) return false;
    Reader r{bytes};
    if (r.get(1) != PROTOCOL_VERSION) return false;
    Frame f;
    f.type = Type(r.get(1));
    if (r.get(2) != bytes.size() - HEADER_SIZE) return false;
    for (auto& c : f.id.node) c = char(r.get(1));
    f.id.boot = r.get(8);
    f.id.seq  = r.get(4);
    if (telemetry(f.type)) {
      f.common.uptime = r.get(4);
      auto occupied   = r.get(1);
      if (occupied > 1) return false;
      f.common.occupied = occupied;
      f.common.battery  = r.get(2);
      f.common.revision = r.get(4);
      f.common.health   = r.get(1);
    }
    // Earlier development snapshots carried only common telemetry; preserve them as facts.
    if (f.type == Type::STATE_SYNC && bytes.size() != HEADER_SIZE + 12)
      f.boot_generation = r.get(8);
    if (f.type == Type::CONFIG_SET || f.type == Type::COMMAND_RESULT ||
        f.type == Type::COMMAND_RECEIPT)
      for (auto& b : f.command) b = r.get(1);
    if (f.type == Type::COMMAND_RESULT || f.type == Type::COMMAND_RECEIPT)
      f.status = Status(r.get(1));
    if (f.type == Type::CONFIG_SET) {
      f.config.revision         = r.get(4);
      f.config.heartbeat_s      = r.get(4);
      f.config.movement_batch_s = r.get(4);
      auto enabled              = r.get(1);
      if (enabled > 1) return false;
      f.config.movement_enabled = enabled;
    } else if (f.type == Type::EVENT_ACK) {
      f.ack_boot = r.get(8);
      f.ack_seq  = r.get(4);
    } else if (f.type == Type::READY) {
      f.reason = Reason(r.get(1));
      f.listen = r.get(4);
    } else if (f.type == Type::MOVEMENT_BATCH) {
      f.start          = r.get(4);
      f.duration       = r.get(4);
      const auto count = r.get(2);
      if (count > MAX_SAMPLES || r.get(1) != 0 || r.get(1) != 0) return false;
      f.end = End(r.get(1));
      if (r.get(1) != 0) return false;
      f.samples.resize(count);
      for (auto& s : f.samples) {
        s.offset = r.get(2);
        s.ax     = int16_t(r.get(2));
        s.ay     = int16_t(r.get(2));
        s.az     = int16_t(r.get(2));
        s.gx     = int16_t(r.get(2));
        s.gy     = int16_t(r.get(2));
        s.gz     = int16_t(r.get(2));
      }
    }
    if (!r.ok || r.pos != bytes.size() || !valid(f)) return false;
    output = std::move(f);
    return true;
  }
}  // namespace SmartChair
