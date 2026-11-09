#include <Arduino.h>

#include "config.h"
#include "driver/gpio.h"
#include "esp_sleep.h"
#include "pins.h"

/**
 * Smart Chair — Deep Sleep & Seat Sensor Wakeup Demo
 * Target: ESP32-C3 SuperMini
 *
 * Hardware Wiring:
 *   - Seat contact wire 1 -> GPIO3 (Pins::SEAT_SENSOR)
 *   - Seat contact wire 2 -> GND
 *
 * Logic:
 *   - Vacant (Switch OPEN)    : GPIO3 is pulled HIGH (3.3V) -> Deep Sleep (~5-10 uA)
 *   - Seated (Switch CLOSED)  : GPIO3 is pulled LOW  (0V)   -> Instant Hardware Wakeup
 */

RTC_DATA_ATTR static uint32_t bootCount = 0;

void enterDeepSleep() {
  Serial.printf("\n[SLEEP] Preparing deep sleep on GPIO%d...\n", Pins::SEAT_SENSOR);
  Serial.printf("[SLEEP] Internal pull-up is active on GPIO%d.\n", Pins::SEAT_SENSOR);
  Serial.printf("[SLEEP] Press seat switch (close GPIO%d to GND) to wake up.\n", Pins::SEAT_SENSOR);
  Serial.flush();
  delay(100);

  // Maintain internal pull-up and pin state during deep sleep
  gpio_pullup_en((gpio_num_t)Pins::SEAT_SENSOR);
  gpio_pulldown_dis((gpio_num_t)Pins::SEAT_SENSOR);
  gpio_hold_en((gpio_num_t)Pins::SEAT_SENSOR);

  // ESP32-C3 RTC GPIO wakeup (GPIO0-GPIO5 supported on LOW)
  esp_deep_sleep_enable_gpio_wakeup(1ULL << Pins::SEAT_SENSOR, ESP_GPIO_WAKEUP_GPIO_LOW);

  esp_deep_sleep_start();
}

void setup() {
  // Release pin hold lock from previous sleep cycle
  gpio_hold_dis((gpio_num_t)Pins::SEAT_SENSOR);

  Serial.begin(115200);
  delay(Config::Timing::SERIAL_ATTACH_WAIT_MS);

  bootCount++;

  esp_sleep_source_t wakeCause = esp_sleep_get_wakeup_cause();
  const char* causeStr         = (wakeCause == ESP_SLEEP_WAKEUP_GPIO)
                                     ? "Seat Contact Closed (GPIO3 Wakeup on LOW)"
                                     : "Power-on / Cold Reset (Cold Boot)";

  Serial.println();
  Serial.println("==================================================");
  Serial.printf("  Smart Chair Demo — %s\n", Pins::BOARD_NAME);
  Serial.printf("  Boot / Wake Cycle : #%lu (persisted in RTC RAM)\n", bootCount);
  Serial.printf("  Wakeup Cause      : %s\n", causeStr);
  Serial.printf("  Seat Input Pin    : GPIO%d (Internal Pull-Up Enabled)\n", Pins::SEAT_SENSOR);
  Serial.printf("  Target Wiring     : GPIO%d <---> Seat Switch <---> GND\n", Pins::SEAT_SENSOR);
  Serial.println("==================================================");

  pinMode(Pins::SEAT_SENSOR, INPUT_PULLUP);

  // Read initial sensor voltage level
  int rawState = digitalRead(Pins::SEAT_SENSOR);

  if (rawState == Config::Sensor::LEVEL_VACANT) {
    Serial.printf("[STATUS] GPIO%d reads HIGH (3.3V) -> Seat is VACANT.\n", Pins::SEAT_SENSOR);
    enterDeepSleep();
  } else {
    Serial.printf("[STATUS] GPIO%d reads LOW (GND) -> Seat is OCCUPIED! Node remains awake.\n",
                  Pins::SEAT_SENSOR);
  }
}

void loop() {
  static unsigned long lastOccupiedLog = 0;
  unsigned long now                    = millis();

  // Check if seat switch has opened (person stood up)
  if (digitalRead(Pins::SEAT_SENSOR) == Config::Sensor::LEVEL_VACANT) {
    Serial.printf("\n[DEBOUNCE] GPIO%d went HIGH. Verifying release (%lu ms)...\n",
                  Pins::SEAT_SENSOR, Config::Timing::DEBOUNCE_MS);
    delay(Config::Timing::DEBOUNCE_MS);

    if (digitalRead(Pins::SEAT_SENSOR) == Config::Sensor::LEVEL_VACANT) {
      Serial.printf("[EVENT] Verified: GPIO%d is stable HIGH -> Seat VACATED.\n",
                    Pins::SEAT_SENSOR);
      enterDeepSleep();
    } else {
      Serial.println("[DEBOUNCE] Signal fluctuated, ignoring glitch.");
    }
  }

  // Periodic status log while occupied
  if (now - lastOccupiedLog >= Config::Timing::HEARTBEAT_INTERVAL_MS) {
    lastOccupiedLog = now;
    Serial.printf("[ACTIVE] GPIO%d = LOW (GND) | State: OCCUPIED | Active Uptime: %lu ms\n",
                  Pins::SEAT_SENSOR, now);
  }

  delay(20);
}
