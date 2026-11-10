#include <Arduino.h>

#include "config.h"

/**
 * Smart Chair — Desk Gateway Receiver
 * Target Board: ESP32-S3 DevKit
 *
 * Role:
 *   - Listens for ESP-NOW events from the chair sensor node.
 *   - Forwards events to MQTT / Raspberry Pi.
 */

void setup() {
  Serial.begin(GatewayConfig::SERIAL_BAUD);
  delay(GatewayConfig::SERIAL_ATTACH_WAIT_MS);

  Serial.println();
  Serial.println("==================================================");
  Serial.println("  Smart Chair — Desk Gateway (ESP32-S3)");
  Serial.printf("  Listening on ESP-NOW Channel : %d\n", GatewayConfig::ESP_NOW_CHANNEL);
  Serial.println("  Status : Ready to receive chair telemetry");
  Serial.println("==================================================");
  Serial.println();
}

void loop() {
  // Gateway packet receiving & forwarding loop (to be implemented with ESP-NOW)
  static unsigned long lastHeartbeat = 0;
  unsigned long now                  = millis();

  if (now - lastHeartbeat >= 5000) {
    lastHeartbeat = now;
    Serial.printf("[GATEWAY] Alive | Uptime: %lu ms\n", now);
  }

  delay(20);
}
