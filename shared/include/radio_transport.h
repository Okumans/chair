#pragma once

#include <cstddef>
#include <cstdint>

#include "transport_constants.h"

namespace Transport {
  /// Initialize USB and validate provisioning. Radio startup is requested separately.
  void begin(const char* role, const char* ownMac, const char* peerMac);
  /// Loop-context packet consumer; never invoked from the radio callback or an interrupt.
  using Receiver = void (*)(const uint8_t*, size_t);
  /// Install the chair protocol consumer; nullptr selects gateway validation and USB forwarding.
  void setReceiver(Receiver receiver);
  /// Suspend or restore Wi-Fi and the encrypted peer without restarting USB.
  bool suspend(bool report = true);
  bool resume(bool report = true);
  /// Gate USB-originated sends while a lifecycle worker owns the radio.
  void setLifecycleBusy(bool busy);
  /// Return whether radio initialization and encrypted peer setup succeeded.
  bool available();
  /// True only when the Wi-Fi hardware has been stopped.
  bool stopped();
  /// A timed-out MAC send requires a radio cycle before another transmission.
  bool needsRestart();
  /// Perform bounded USB work, drain callback queues and resume partial output writes.
  void poll();
  /// Return whether one MAC transmission is still awaiting completion or its timeout.
  bool busy();
  /// Submit 1–1470 bytes if ready and idle. True means queued, not application delivery.
  /// requestId associates USB replies; zero marks automatic chair-originated sends.
  bool send(const uint8_t* data, size_t size, uint32_t requestId = 0);
}  // namespace Transport
