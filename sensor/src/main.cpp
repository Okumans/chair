#include <Arduino.h>

#include "config.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "json_messages.h"
#include "link_config.h"
#include "pins.h"
#include "radio_transport.h"

static_assert(static_cast<uint8_t>(Pins::SEAT_SENSOR) == 5, "C3 deep-sleep wake requires GPIO0–5");

namespace {
  int candidate          = HIGH;
  int stable             = -1;
  uint32_t changedAt     = 0;
  uint32_t lastReport    = 0;
  bool reportQueued      = false;
  const char* wakeReason = "reset";

  void sleepWhileVacant() {
    if (esp_deep_sleep_enable_gpio_wakeup(1ULL << Pins::SEAT_SENSOR, ESP_GPIO_WAKEUP_GPIO_LOW) !=
            ESP_OK ||
        esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(Config::Timing::HEARTBEAT_INTERVAL_MS) *
                                      1000) != ESP_OK) {
      Transport::printJson(
          glz::obj{"kind", "error", "request_id", 0, "reason", "sleep_setup_failed"});
      reportQueued = false;
      return;
    }

    Transport::printJson(glz::obj{"kind", "sleep", "wake_pin", Pins::SEAT_SENSOR, "heartbeat_ms",
                                  Config::Timing::HEARTBEAT_INTERVAL_MS});
    Serial.flush();
    // A closure during sleep preparation must still be reported before going back to sleep.
    if (digitalRead(Pins::SEAT_SENSOR) != Config::Sensor::LEVEL_VACANT) return;
    if (esp_wifi_stop() != ESP_OK) {
      Transport::printJson(
          glz::obj{"kind", "error", "request_id", 0, "reason", "radio_stop_failed"});
      reportQueued = false;
      return;
    }
    esp_deep_sleep_start();
  }
}  // namespace

void setup() {
  pinMode(Pins::SEAT_SENSOR, INPUT_PULLUP);

  const esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  if (cause == ESP_SLEEP_WAKEUP_GPIO) wakeReason = "gpio";
  if (cause == ESP_SLEEP_WAKEUP_TIMER) wakeReason = "timer";

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
    std::string info;
    const auto message = glz::obj{"seat_pin",    Pins::SEAT_SENSOR,
                                  "occupancy",   candidate == LOW ? "occupied" : "vacant",
                                  "uptime_ms",   now,
                                  "wake_reason", wakeReason};
    if (glz::write_json(message, info)) {
      reportQueued = false;
      Transport::printJson(
          glz::obj{"kind", "error", "request_id", 0, "reason", "json_encode_failed"});
    } else {
      reportQueued = Transport::send(reinterpret_cast<const uint8_t*>(info.data()), info.size());
    }
    stable     = candidate;
    lastReport = now;
  }

  if (reportQueued && stable == Config::Sensor::LEVEL_VACANT && candidate == stable &&
      now - changedAt >= Config::Timing::DEBOUNCE_MS) {
    if (!Transport::busy()) {
      sleepWhileVacant();
    } else if (now - lastReport >= Config::Timing::TX_WAIT_TIMEOUT_MS) {
      Transport::printJson(
          glz::obj{"kind", "error", "request_id", 0, "reason", "radio_send_timeout"});
      sleepWhileVacant();
    }
  }

  delay(2);
}
