#include <Arduino.h>

#include "chair_constants.h"
#include "esp_timer.h"
#include "link_config.h"
#include "radio_transport.h"

namespace {
  uint64_t nextRetry = 0;
}

/// Initialize the encrypted peer and typed USB bridge.
void setup() {
  Transport::begin(Transport::GATEWAY_ROLE, LinkConfig::GATEWAY_MAC, LinkConfig::SENSOR_MAC);
  Transport::resume();
}

/// Drain bounded radio/USB work while leaving scheduling to the cooperative loop.
void loop() {
  Transport::poll();
  const uint64_t now = static_cast<uint64_t>(esp_timer_get_time()) /
                       SmartChair::Timing::MICROSECONDS_PER_MILLISECOND;
  if ((!Transport::available() || Transport::needsRestart()) && !Transport::busy() &&
      now >= nextRetry) {
    nextRetry = SmartChair::deadlineAfter(now, SmartChair::Timing::RADIO_RETRY_MS);
    if (!Transport::needsRestart() || Transport::suspend()) Transport::resume();
  }
  delay(Transport::POLL_DELAY_MS);
}
