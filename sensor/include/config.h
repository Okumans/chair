#pragma once
#include <cstdint>

/**
 * Smart Chair — Application & Timing Configurations
 */

namespace Config {

  namespace Timing {
    // Contact debounce duration to filter mechanical chatter on sit/stand
    constexpr uint32_t DEBOUNCE_MS           = 50;

    // Wakes a vacant sleeping node
    constexpr uint32_t HEARTBEAT_INTERVAL_MS = 900000;

    // Bound awake time if a radio completion callback never arrives.
    constexpr uint32_t TX_WAIT_TIMEOUT_MS    = 1000;
  }  // namespace Timing

  namespace Sensor {
    // Digital dry contact logic (normally open switch to GND)
    constexpr uint8_t LEVEL_OCCUPIED = 0;  // LOW
    constexpr uint8_t LEVEL_VACANT   = 1;  // HIGH
  }  // namespace Sensor

  namespace Power {
    // Li-ion 1S thresholds (in millivolts)
    constexpr uint16_t BATTERY_FULL_MV = 4200;
    constexpr uint16_t BATTERY_LOW_MV  = 3300;
  }  // namespace Power

  namespace Protocol {
    constexpr uint8_t VERSION             = 1;
    constexpr const char* DEFAULT_NODE_ID = "chair-01";
  }  // namespace Protocol

}  // namespace Config
