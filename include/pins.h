#pragma once
#include <cstdint>

/**
 * Smart Chair — Pinout Definitions
 *
 * Target boards are selected via build_flags in platformio.ini:
 *   - BOARD_C3_SUPERMINI: ESP32-C3 SuperMini compact prototype
 *   - BOARD_S3_BENCH:     ESP32-S3 DevKit bench test board
 */

namespace Pins {

#if defined(BOARD_C3_SUPERMINI)
  // Seat occupancy switch (dry contact to GND with internal/external pull-up)
  constexpr uint8_t SEAT_SENSOR    = 3;

  // I2C bus (GY-521 MPU6050 / LSM6DS3)
  constexpr uint8_t I2C_SDA        = 6;
  constexpr uint8_t I2C_SCL        = 7;

  // Optional sensor interrupt & battery ADC
  constexpr uint8_t IMU_INT        = 4;
  constexpr uint8_t BATTERY_ADC    = 0;

  constexpr const char* BOARD_NAME = "ESP32-C3 SuperMini";

#elif defined(BOARD_S3_BENCH)
  // Seat occupancy switch
  constexpr uint8_t SEAT_SENSOR    = 15;

  // I2C bus
  constexpr uint8_t I2C_SDA        = 8;
  constexpr uint8_t I2C_SCL        = 9;

  // Optional sensor interrupt & battery ADC
  constexpr uint8_t IMU_INT        = 10;
  constexpr uint8_t BATTERY_ADC    = 1;

  constexpr const char* BOARD_NAME = "ESP32-S3 DevKit Bench";

#else
#error "Target board not recognized. Set BOARD_C3_SUPERMINI or BOARD_S3_BENCH in platformio.ini."
#endif

}  // namespace Pins
