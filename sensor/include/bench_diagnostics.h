#pragma once

#include "chair_engine.h"
#include "json_messages.h"

#ifndef CHAIR_DIAGNOSTICS
#define CHAIR_DIAGNOSTICS 0
#endif
#ifndef CHAIR_RADIO_START_DELAY_MS
#define CHAIR_RADIO_START_DELAY_MS 0
#endif

namespace Bench {
  inline constexpr uint32_t START_DELAY_MS = CHAIR_RADIO_START_DELAY_MS;

  struct Trace {
    std::string_view kind = "sensor_trace";
    std::string_view event;
    std::string boot_id;
    uint64_t uptime_ms;
    std::string_view wake_reason;
    std::optional<bool> raw_occupied;
    bool occupied;
    bool moving;
    bool storage_failed;
    std::string_view radio_state;
    uint64_t closes_ms;
  };

  /// Bench builds only: raw edges and confirmed state share the same acquisition clock.
  inline void trace(std::string_view event, const SmartChair::Engine& engine, uint64_t now,
                    SmartChair::Wake wake, std::optional<bool> raw = std::nullopt) {
    if constexpr (CHAIR_DIAGNOSTICS) {
      const auto state = engine.communication.state;
      Transport::printJson(Trace{
          .event          = event,
          .boot_id        = SmartChair::hex64(engine.identity.boot),
          .uptime_ms      = now,
          .wake_reason    = wake == SmartChair::Wake::seat    ? "seat"
                            : wake == SmartChair::Wake::timer ? "timer"
                                                              : "cold_boot",
          .raw_occupied   = raw,
          .occupied       = engine.occupied,
          .moving         = engine.motionActive(),
          .storage_failed = engine.storageFailed,
          .radio_state    = state == SmartChair::Communication::CLOSED    ? "closed"
                            : state == SmartChair::Communication::OPENING ? "opening"
                            : state == SmartChair::Communication::OPEN    ? "open"
                                                                          : "settling",
          .closes_ms      = engine.communication.closes,
      });
    }
  }
}  // namespace Bench
