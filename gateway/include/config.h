#pragma once
#include <cstdint>

/**
 * Smart Chair — Desk Gateway Configuration
 * Target Board: ESP32-S3 DevKit
 */

namespace GatewayConfig {
  constexpr uint32_t SERIAL_BAUD           = 115200;
  constexpr uint32_t SERIAL_ATTACH_WAIT_MS = 1500;
  constexpr uint8_t ESP_NOW_CHANNEL        = 1;
}  // namespace GatewayConfig
