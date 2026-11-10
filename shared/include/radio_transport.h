#pragma once

#include <cstddef>
#include <cstdint>

namespace Transport {
  void begin(const char* role, const char* ownMac, const char* peerMac);
  void poll();
  bool busy();
  bool send(const uint8_t* data, size_t size, uint32_t requestId = 0);
}  // namespace Transport
