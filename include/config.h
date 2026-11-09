#pragma once
#include <cstdint>

/**
 * Smart Chair — Application & Timing Configurations
 */

namespace Config {

  namespace Timing {
    // Contact debounce duration to filter mechanical chatter on sit/stand
    constexpr uint32_t DEBOUNCE_MS           = 50;

    // Periodic heartbeat / telemetry interval in active state
    constexpr uint32_t HEARTBEAT_INTERVAL_MS = 2000;

    // Wait time for native USB CDC attachment after power on
    constexpr uint32_t SERIAL_ATTACH_WAIT_MS = 1500;
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
