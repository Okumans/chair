#pragma once

#include "protocol.h"

namespace Mpu6050 {
  /// Initialize I2C and MPU6050 at 25 Hz, ±2 g, ±250 degrees/s; false means unavailable.
  bool begin();
  /// Read six raw signed axes. False means I2C failed; sample is unchanged on failure.
  bool read(SmartChair::Sample& sample);
}  // namespace Mpu6050
