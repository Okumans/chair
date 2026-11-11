#pragma once
#include <cstdint>
#include <cstdio>
#include <string>

constexpr int LOW          = 0;
constexpr int HIGH         = 1;
constexpr int INPUT_PULLUP = 2;

struct FakeSerial {
  template <typename... Args>
  void printf(const char*, Args...) {}

  void println(const char*) {}

  std::string output;

  void print(const char* text) { output += text; }

  void print(char c) { output += c; }

  void flush();
};

extern FakeSerial Serial;
uint32_t millis();
void delay(uint32_t ms);
void pinMode(uint8_t pin, int mode);
int digitalRead(uint8_t pin);
#define RTC_DATA_ATTR
uint32_t esp_random();
