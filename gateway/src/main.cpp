#include <Arduino.h>

#include "link_config.h"
#include "radio_transport.h"

/// Initialize the encrypted peer and typed USB bridge.
void setup() {
  Transport::begin(Transport::GATEWAY_ROLE, LinkConfig::GATEWAY_MAC, LinkConfig::SENSOR_MAC);
}

/// Drain bounded radio/USB work while leaving scheduling to the cooperative loop.
void loop() {
  Transport::poll();
  delay(Transport::POLL_DELAY_MS);
}
