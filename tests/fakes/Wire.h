#pragma once
#include <cstddef>
#include <cstdint>

struct FakeWire {
  uint8_t reg      = 0;
  unsigned written = 0, cursor = 0;

  void begin(int, int) {}

  void setTimeOut(int) {}

  void beginTransmission(uint8_t) { written = 0; }

  void write(uint8_t value) {
    if (!written++) reg = value;
  }

  int endTransmission(bool = true) { return 0; }

  int requestFrom(uint8_t, uint8_t count) {
    cursor = 0;
    return count;
  }

  int read() {
    if (reg == 0x75) return 0x68;
    return cursor++ == 4 ? 0x40 : 0;
  }
};

inline FakeWire Wire;
