#include "communication_window.h"

#include <algorithm>

namespace SmartChair {
  void Window::open(uint64_t length) {
    state     = Communication::OPENING;
    duration  = length;
    unlimited = length == 0;
  }

  void Window::radioReady(uint64_t now) {
    if (state == Communication::OPENING) {
      state  = Communication::OPEN;
      closes = now + duration;
    }
  }

  void Window::extend(uint64_t now, uint64_t length) {
    if (state == Communication::CLOSED) {
      open(length);
    } else if (state == Communication::OPENING) {
      duration  = std::max(duration, length);
      unlimited = unlimited || length == 0;
    } else {
      unlimited = unlimited || length == 0;
      state     = Communication::OPEN;
      closes    = std::max(closes, now + length);
    }
  }

  void Window::settle(uint64_t now) {
    state    = Communication::SETTLING;
    deadline = now + 2500;
  }

  void Window::tick(uint64_t now, bool pending) {
    if (state == Communication::OPEN && !unlimited && now >= closes) settle(now);
    if (state == Communication::SETTLING && (!pending || now >= deadline))
      state = Communication::CLOSED;
  }

  uint32_t Window::remaining(uint64_t now) const {
    return unlimited
               ? UINT32_MAX
               : uint32_t(std::min<uint64_t>(UINT32_MAX - 1, closes > now ? closes - now : 0));
  }
}  // namespace SmartChair
