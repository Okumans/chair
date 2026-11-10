#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace Transport {
  constexpr size_t MAX_PAYLOAD = 250;
  constexpr size_t MAX_LINE    = 2048;

  inline int hexDigit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  }

  inline bool decodeHex(const char* text, uint8_t* bytes, size_t capacity, size_t& size) {
    const size_t length = strlen(text);
    if (length % 2 || length / 2 > capacity) return false;

    for (size_t i = 0; i < length / 2; ++i) {
      const int high = hexDigit(text[i * 2]);
      const int low  = hexDigit(text[i * 2 + 1]);
      if (high < 0 || low < 0) return false;
      bytes[i] = (high << 4) | low;
    }
    size = length / 2;
    return true;
  }

  inline void encodeHex(const uint8_t* bytes, size_t size, char* text) {
    constexpr char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) {
      text[i * 2]     = digits[bytes[i] >> 4];
      text[i * 2 + 1] = digits[bytes[i] & 15];
    }
    text[size * 2] = '\0';
  }

  inline bool decodeMac(const char* text, uint8_t* bytes) {
    if (strlen(text) != 17) return false;
    for (size_t i = 0; i < 6; ++i) {
      const int high = hexDigit(text[i * 3]);
      const int low  = hexDigit(text[i * 3 + 1]);
      if (high < 0 || low < 0 || (i < 5 && text[i * 3 + 2] != ':')) return false;
      bytes[i] = (high << 4) | low;
    }
    return (bytes[0] & 1) == 0 && memcmp(bytes, "\0\0\0\0\0\0", 6) != 0;
  }

  // Returns one bounded line; overlong input is discarded through the next newline.
  class LineReader {
   public:
    bool push(char c) {
      if (c == '\n') {
        const bool ready = !overflow && used > 0;
        if (used && buffer[used - 1] == '\r') --used;
        buffer[used] = '\0';
        used         = 0;
        overflow     = false;
        return ready;
      }
      if (used == MAX_LINE || c == '\0') overflow = true;
      if (!overflow) buffer[used++] = c;
      return false;
    }

    void reset() {
      used     = 0;
      overflow = false;
    }
    const char* line() const { return buffer; }

   private:
    char buffer[MAX_LINE + 1]{};
    size_t used   = 0;
    bool overflow = false;
  };
}  // namespace Transport
