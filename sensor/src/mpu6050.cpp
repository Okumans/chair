#include "mpu6050.h"

#include <Wire.h>

#include "pins.h"

namespace Mpu6050 {
  namespace {
    constexpr uint8_t I2C_ADDRESS          = 0x68;
    constexpr uint8_t EXPECTED_DEVICE_ID   = 0x68;
    constexpr uint16_t BUS_TIMEOUT_MS      = 5;
    constexpr uint8_t ID_BYTES             = 1;
    constexpr uint8_t SAMPLE_BYTES         = 14;
    constexpr uint8_t PLL_X_GYRO_CLOCK     = 0x01;
    constexpr uint8_t FILTER_44_HZ         = 0x03;
    constexpr uint8_t SAMPLE_DIVIDER_25_HZ = 39;
    constexpr uint8_t ACCEL_RANGE_2_G      = 0;
    constexpr uint8_t GYRO_RANGE_250_DPS   = 0;

    enum class Register : uint8_t {
      SAMPLE_DIVIDER = 0x19,
      FILTER         = 0x1a,
      GYRO_RANGE     = 0x1b,
      ACCEL_RANGE    = 0x1c,
      ACCEL_DATA     = 0x3b,
      POWER          = 0x6b,
      DEVICE_ID      = 0x75,
    };

    bool writeImu(Register reg, uint8_t value) {
      Wire.beginTransmission(I2C_ADDRESS);
      Wire.write(static_cast<uint8_t>(reg));
      Wire.write(value);

      return Wire.endTransmission() == 0;
    }

  }  // namespace

  bool begin() {
    Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
    Wire.setTimeOut(BUS_TIMEOUT_MS);
    Wire.beginTransmission(I2C_ADDRESS);
    Wire.write(static_cast<uint8_t>(Register::DEVICE_ID));

    if (Wire.endTransmission(false) || Wire.requestFrom(I2C_ADDRESS, ID_BYTES) != ID_BYTES ||
        Wire.read() != EXPECTED_DEVICE_ID)
      return false;

    // ±2 g, ±250 degrees/s, 44 Hz filter and 25 Hz internal sample rate.
    return writeImu(Register::POWER, PLL_X_GYRO_CLOCK) &&
           writeImu(Register::FILTER, FILTER_44_HZ) &&
           writeImu(Register::SAMPLE_DIVIDER, SAMPLE_DIVIDER_25_HZ) &&
           writeImu(Register::GYRO_RANGE, GYRO_RANGE_250_DPS) &&
           writeImu(Register::ACCEL_RANGE, ACCEL_RANGE_2_G);
  }

  bool read(SmartChair::Sample& s) {
    Wire.beginTransmission(I2C_ADDRESS);
    Wire.write(static_cast<uint8_t>(Register::ACCEL_DATA));

    if (Wire.endTransmission(false) || Wire.requestFrom(I2C_ADDRESS, SAMPLE_BYTES) != SAMPLE_BYTES)
      return false;

    auto readWord = []() {
      const int high = Wire.read();
      return int16_t((high << 8) | Wire.read());
    };

    s.ax = readWord();
    s.ay = readWord();
    s.az = readWord();
    readWord();
    s.gx = readWord();
    s.gy = readWord();
    s.gz = readWord();

    return true;
  }

}  // namespace Mpu6050
