#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace SmartChair {
  /// Deadlines saturate instead of wrapping when supplied time approaches uint64 exhaustion.
  constexpr uint64_t deadlineAfter(uint64_t now, uint64_t delay) {
    return delay > UINT64_MAX - now ? UINT64_MAX : now + delay;
  }

  /// Internal cooperative timing in monotonic milliseconds; not downstream configuration fields.
  namespace Timing {
    inline constexpr uint64_t MILLISECONDS_PER_SECOND      = 1000;
    inline constexpr uint64_t MICROSECONDS_PER_MILLISECOND = 1000;
    inline constexpr uint64_t SEAT_DEBOUNCE_MS             = 50;
    inline constexpr uint64_t SAMPLE_INTERVAL_MS           = 40;
    inline constexpr uint64_t MOTION_DEBOUNCE_MS           = 100;
    inline constexpr uint64_t DELIVERY_ROUND_MS            = 2500;
    inline constexpr uint64_t RADIO_START_TIMEOUT_MS       = 2500;
    inline constexpr uint64_t RADIO_STOP_TIMEOUT_MS        = 2500;
    inline constexpr uint64_t RADIO_RETRY_MS               = 1000;
    inline constexpr uint64_t STORAGE_RETRY_MS             = 250;
    inline constexpr std::array<uint64_t, 4> RETRY_OFFSETS_MS{0, 250, 750, 1750};
  }  // namespace Timing

  /// Fixed RAM and durable bounds; one outbox slot always remains available for state recovery.
  namespace Limits {
    inline constexpr size_t OUTBOX_CAPACITY     = 32;
    inline constexpr size_t COMMAND_RECORDS     = 32;
    inline constexpr size_t STATE_SYNC_RESERVED = 1;
    inline constexpr size_t MAX_COMMAND_RESULTS = OUTBOX_CAPACITY - STATE_SYNC_RESERVED;
    inline constexpr size_t BEST_EFFORT_QUEUE   = 4;
    inline constexpr size_t STORED_STATE_BYTES  = 8192;
    inline constexpr uint32_t SEQUENCE_RESERVE  = 128;
  }  // namespace Limits

  /// Complete factory settings; accepted timing values still span every nonzero uint32 value.
  namespace Defaults {
    inline constexpr uint32_t HEARTBEAT_S         = 900;
    inline constexpr uint32_t MOVEMENT_BATCH_S    = 3;
    inline constexpr bool MOVEMENT_ENABLED        = true;
    inline constexpr bool RADIO_WINDOWS_ENABLED   = true;
    inline constexpr uint32_t RADIO_INTERVAL_S    = 30;
    inline constexpr uint32_t RADIO_OPEN_S        = 5;
    inline constexpr uint32_t RADIO_MOTION_TAIL_S = 10;
  }  // namespace Defaults

  /// MPU6050 scaling and initial classification thresholds, kept separate from reported raw axes.
  namespace MotionThresholds {
    inline constexpr double ACCEL_COUNTS_PER_G  = 16384.0;
    inline constexpr double GYRO_COUNTS_PER_DPS = 131.0;
    inline constexpr double START_ACCEL_G       = 0.15;
    inline constexpr double QUIET_ACCEL_G       = 0.08;
    inline constexpr double START_GYRO_DPS      = 15.0;
    inline constexpr double QUIET_GYRO_DPS      = 8.0;
  }  // namespace MotionThresholds

  /// Defined v1 health bits. All other bits are reserved and rejected by the decoder.
  namespace Health {
    inline constexpr uint8_t IMU_UNAVAILABLE     = 1 << 0;
    inline constexpr uint8_t BATTERY_UNAVAILABLE = 1 << 1;
    inline constexpr uint8_t KNOWN_FLAGS         = IMU_UNAVAILABLE | BATTERY_UNAVAILABLE;
  }  // namespace Health
}  // namespace SmartChair
