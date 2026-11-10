#pragma once
#include <cstdint>

/**
 * Smart Chair — Common ESP-NOW Protocol Definitions
 * Shared between Chair Sensor Node and Desk Gateway
 */

namespace SmartChair {

  constexpr uint8_t PROTOCOL_VERSION = 1;

  enum class EventType : uint8_t {
    OCCUPIED   = 1,
    VACATED    = 2,
    HEARTBEAT  = 3,
    STATE_SYNC = 4,
  };

  struct __attribute__((packed)) Message {
    uint8_t version;
    char nodeId[16];
    uint32_t seq;
    EventType event;
    uint16_t batteryMv;
    uint32_t uptimeMs;
  };

}  // namespace SmartChair
