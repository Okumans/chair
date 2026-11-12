#pragma once
#include <cstdint>
#include <cstring>
#include <vector>

struct Preferences {
  inline static bool available = true;
  inline static std::vector<uint8_t> bytes;

  bool begin(const char*, bool) { return available; }

  bool isKey(const char*) { return !bytes.empty(); }

  bool remove(const char*) {
    if (!available) return false;
    bytes.clear();
    return true;
  }

  void end() {}

  size_t getBytesLength(const char*) { return bytes.size(); }

  size_t getBytes(const char*, void* out, size_t size) {
    memcpy(out, bytes.data(), size);
    return size;
  }

  size_t putBytes(const char*, const void* data, size_t size) {
    if (!available) return 0;
    auto p = static_cast<const uint8_t*>(data);
    bytes.assign(p, p + size);
    return size;
  }
};
