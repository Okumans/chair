#pragma once

#include "protocol.h"

namespace Mpu6050 {
  /// Delay between I2C reprobes when the sensor is unavailable.
  inline constexpr uint64_t RETRY_INTERVAL_MS = 5000;

  /// Initialize I2C and MPU6050 at 25 Hz, ±2 g, ±250 degrees/s; false means unavailable.
  bool begin();
  /// Read six raw signed axes. False means I2C failed; sample is unchanged on failure.
  bool read(SmartChair::Sample& sample);
}  // namespace Mpu6050
