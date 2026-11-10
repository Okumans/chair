#pragma once
#include <cstdint>
#include <cstdio>

constexpr int LOW          = 0;
constexpr int HIGH         = 1;
constexpr int INPUT_PULLUP = 2;

struct FakeSerial {
  template <typename... Args>
  void printf(const char*, Args...) {}
  void println(const char*) {}
  void flush();
};
extern FakeSerial Serial;
uint32_t millis();
void delay(uint32_t ms);
void pinMode(uint8_t pin, int mode);
int digitalRead(uint8_t pin);
