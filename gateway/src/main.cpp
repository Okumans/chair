#include <Arduino.h>

#include "link_config.h"
#include "radio_transport.h"

void setup() { Transport::begin("gateway", LinkConfig::GATEWAY_MAC, LinkConfig::SENSOR_MAC); }

void loop() {
  Transport::poll();
  delay(2);
}
