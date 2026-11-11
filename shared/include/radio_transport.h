#pragma once

#include <cstddef>
#include <cstdint>

namespace Transport {
  /// Initialize USB and the encrypted provisioned unicast peer; report failures as structured JSON.
  void begin(const char* role, const char* ownMac, const char* peerMac);
  /// Loop-context packet consumer; never invoked from the radio callback or an interrupt.
  using Receiver = void (*)(const uint8_t*, size_t);
  /// Install the chair protocol consumer; nullptr selects gateway validation and USB forwarding.
  void setReceiver(Receiver receiver);
  /// Return whether radio initialization and encrypted peer setup succeeded.
  bool available();
  /// Perform bounded USB work, drain callback queues and resume partial output writes.
  void poll();
  /// Return whether one MAC transmission is still awaiting completion or its timeout.
  bool busy();
  /// Submit 1–1470 bytes if ready and idle. True means queued, not application delivery.
  /// requestId associates USB replies; zero marks automatic chair-originated sends.
  bool send(const uint8_t* data, size_t size, uint32_t requestId = 0);
}  // namespace Transport
