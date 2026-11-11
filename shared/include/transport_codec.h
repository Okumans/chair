#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string_view>

namespace Transport {
  constexpr size_t MAX_PAYLOAD = 1470;
  constexpr size_t MAX_LINE    = 8192;

  /// Decode one ASCII hex digit, returning -1 for invalid input.
  inline int hexDigit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  }

  /// Decode complete byte pairs within capacity. Ignore bytes and size on failure.
  inline bool decodeHex(std::string_view text, uint8_t* bytes, size_t capacity, size_t& size) {
    const size_t length = text.size();
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

  /// Write lowercase hex plus NUL; text must hold at least size*2+1 bytes.
  inline void encodeHex(const uint8_t* bytes, size_t size, char* text) {
    constexpr char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) {
      text[i * 2]     = digits[bytes[i] >> 4];
      text[i * 2 + 1] = digits[bytes[i] & 15];
    }
    text[size * 2] = '\0';
  }

  /// Decode a nonzero unicast station MAC in colon-separated hexadecimal form.
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
  /// Bounded LF/CRLF accumulator; discard overlong or NUL-bearing input through newline.
  class LineReader {
   public:
    /// Consume a byte. True exposes a complete line until the next push; false means
    /// incomplete/invalid.
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

    /// Discard the partial line and overflow state when a USB connection changes.
    void reset() {
      used     = 0;
      overflow = false;
    }

    /// Return the last completed, NUL-terminated line; consume it before the next push.
    const char* line() const { return buffer; }

   private:
    char buffer[MAX_LINE + 1]{};
    size_t used   = 0;
    bool overflow = false;
  };
}  // namespace Transport
