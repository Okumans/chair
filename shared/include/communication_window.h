#pragma once

#include <cstdint>

namespace SmartChair {
  /// Independent radio lifecycle; only CLOSED permits future occupied sleep.
  enum class Communication { CLOSED, OPENING, OPEN, SETTLING };

  /// Radio-window timing model. All times are monotonic uint64 milliseconds.
  struct Window {
    Communication state = Communication::CLOSED;
    uint64_t duration = 0, closes = 0, deadline = 0;
    bool unlimited = false;

    /// Request radio opening. A zero duration means indefinitely open while awake.
    void open(uint64_t length = 0);

    /// Start the requested window when the hardware reports the radio ready.
    void radioReady(uint64_t now);

    /// Open or extend availability by length milliseconds; opening time is not charged to it.
    void extend(uint64_t now, uint64_t length);

    /// Begin bounded shutdown, allowing at most 2500 ms for pending work.
    void settle(uint64_t now);

    /// Advance expiry and shutdown; pending work may delay closing only until the deadline.
    void tick(uint64_t now, bool pending);

    /// Return true only when the radio lifecycle is fully closed.
    bool allowsSleep() const { return state == Communication::CLOSED; }

    /// Compute listen time at transmission; UINT32_MAX means no scheduled close.
    uint32_t remaining(uint64_t now) const;
  };

}  // namespace SmartChair
