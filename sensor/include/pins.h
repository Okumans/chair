#pragma once
#include <cstdint>

/**
 * Smart Chair — Pinout Definitions
 * Target Board: ESP32-C3 SuperMini
 */

namespace Pins {

  // Seat occupancy switch (dry contact to GND with internal pull-up)
  constexpr uint8_t SEAT_SENSOR = 5;

  // I2C bus (GY-521 MPU6050)
  constexpr uint8_t I2C_SDA = 6;
  constexpr uint8_t I2C_SCL = 7;

  // Optional sensor interrupt & battery ADC
  constexpr uint8_t IMU_INT        = 21;
  constexpr uint8_t BATTERY_ADC    = 0;

  constexpr const char* BOARD_NAME = "ESP32-C3 SuperMini";

}  // namespace Pins
