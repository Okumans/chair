#include "mpu6050.h"

#include <Wire.h>

#include "pins.h"

namespace Mpu6050 {
  namespace {
    bool writeImu(uint8_t reg, uint8_t value) {
      Wire.beginTransmission(0x68);
      Wire.write(reg);
      Wire.write(value);

      return Wire.endTransmission() == 0;
    }

  }  // namespace

  bool begin() {
    Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
    Wire.setTimeOut(5);
    Wire.beginTransmission(0x68);
    Wire.write(0x75);

    if (Wire.endTransmission(false) || Wire.requestFrom(uint8_t(0x68), uint8_t(1)) != 1 ||
        Wire.read() != 0x68)
      return false;

    // ±2 g, ±250 degrees/s, 44 Hz filter and 25 Hz internal sample rate.
    return writeImu(0x6b, 0x01) && writeImu(0x1a, 0x03) && writeImu(0x19, 39) &&
           writeImu(0x1b, 0) && writeImu(0x1c, 0);
  }

  bool read(SmartChair::Sample& s) {
    Wire.beginTransmission(0x68);
    Wire.write(0x3b);

    if (Wire.endTransmission(false) || Wire.requestFrom(uint8_t(0x68), uint8_t(14)) != 14)
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
