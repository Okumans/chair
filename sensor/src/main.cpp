#include <Arduino.h>

#include "config.h"
#include "link_config.h"
#include "pins.h"
#include "radio_transport.h"

namespace {
  int candidate       = HIGH;
  int stable          = -1;
  uint32_t changedAt  = 0;
  uint32_t lastReport = 0;
}  // namespace

void setup() {
  pinMode(Pins::SEAT_SENSOR, INPUT_PULLUP);
  Transport::begin("sensor", LinkConfig::SENSOR_MAC, LinkConfig::GATEWAY_MAC);
}

void loop() {
  Transport::poll();
  const uint32_t now = millis();
  const int level    = digitalRead(Pins::SEAT_SENSOR);
  if (level != candidate) {
    candidate = level;
    changedAt = now;
  }

  if (now - changedAt >= Config::Timing::DEBOUNCE_MS &&
      (stable != candidate || now - lastReport >= Config::Timing::HEARTBEAT_INTERVAL_MS)) {
    char info[128];
    const int size =
        snprintf(info, sizeof(info), "{\"seat_pin\":%u,\"occupancy\":\"%s\",\"uptime_ms\":%lu}",
                 Pins::SEAT_SENSOR, candidate == LOW ? "occupied" : "vacant",
                 static_cast<unsigned long>(now));
    Transport::send(reinterpret_cast<const uint8_t*>(info), size);
    stable     = candidate;
    lastReport = now;
  }
  // Bench transport stays awake to receive arbitrary packets; no sleep policy yet.
  delay(2);
}
