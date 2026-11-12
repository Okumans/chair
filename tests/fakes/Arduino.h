#pragma once
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <string>

constexpr int LOW          = 0;
constexpr int HIGH         = 1;
constexpr int INPUT_PULLUP = 2;

struct FakeSerial {
  bool connected = true;
  uint32_t baud  = 0;

  void begin(uint32_t rate) { baud = rate; }

  explicit operator bool() const { return connected; }

  template <typename... Args>
  void printf(const char* format, Args... args) {
    const int length = std::snprintf(nullptr, 0, format, args...);
    assert(length >= 0);
    std::string buffer(static_cast<size_t>(length) + 1, '\0');
    std::snprintf(buffer.data(), buffer.size(), format, args...);
    output.append(buffer.data(), static_cast<size_t>(length));
  }

  void println(const char* text) { output += std::string(text) + "\n"; }

  std::deque<char> input;

  int available() const { return input.size(); }

  int read() {
    const int value = input.front();
    input.pop_front();
    return value;
  }

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
