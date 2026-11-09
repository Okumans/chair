#include <Arduino.h>

#include "config.h"
#include "pins.h"

/**
 * Smart Chair — Seat Occupancy Detection
 *
 * Hardware wiring:
 *   - Seat contact wire 1 -> Pins::SEAT_SENSOR (GPIO3 on C3 / GPIO15 on S3)
 *   - Seat contact wire 2 -> GND
 *
 * Behavior:
 *   - Open contact (unweighted) -> HIGH (VACANT)
 *   - Closed contact (seated)   -> LOW  (OCCUPIED)
 */

int lastStableState               = -1;
int lastRawState                  = -1;
unsigned long lastDebounceTime    = 0;
unsigned long lastPeriodicLogTime = 0;

void printState(int state, const char* prefix = "") {
  const bool isOccupied = (state == Config::Sensor::LEVEL_OCCUPIED);
  Serial.printf("[%lu ms] %sState: %s (GPIO%d = %s)\n", millis(), prefix,
                isOccupied ? "OCCUPIED" : "VACANT", Pins::SEAT_SENSOR,
                state == LOW ? "LOW" : "HIGH");
}

void setup() {
  Serial.begin(115200);

  // Allow USB CDC time to attach
  delay(Config::Timing::SERIAL_ATTACH_WAIT_MS);

  pinMode(Pins::SEAT_SENSOR, INPUT_PULLUP);

  int initialRead = digitalRead(Pins::SEAT_SENSOR);
  lastStableState = initialRead;
  lastRawState    = initialRead;

  Serial.println();
  Serial.println("==========================================");
  Serial.printf("  Smart Chair — %s\n", Pins::BOARD_NAME);
  Serial.printf("  Seat Input Pin : GPIO%d (INPUT_PULLUP)\n", Pins::SEAT_SENSOR);
  Serial.println("  Wiring         : Pin -> Switch -> GND");
  Serial.println("  Logic          : HIGH = VACANT, LOW = OCCUPIED");
  Serial.println("==========================================");
  Serial.println();

  printState(lastStableState, "Initial ");
}

void loop() {
  int currentRaw    = digitalRead(Pins::SEAT_SENSOR);
  unsigned long now = millis();

  // Reset debounce timer on any edge
  if (currentRaw != lastRawState) {
    lastDebounceTime = now;
    lastRawState     = currentRaw;
  }

  // Evaluate state once signal has settled past debounce threshold
  if ((now - lastDebounceTime) > Config::Timing::DEBOUNCE_MS) {
    if (currentRaw != lastStableState) {
      lastStableState = currentRaw;
      printState(lastStableState, "Transition -> ");
    }
  }

  // Periodic heartbeat log
  if (now - lastPeriodicLogTime >= Config::Timing::HEARTBEAT_INTERVAL_MS) {
    lastPeriodicLogTime = now;
    printState(lastStableState, "Heartbeat  -> ");
  }
}
