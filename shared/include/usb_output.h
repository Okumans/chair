#pragma once
#include <deque>
#include <string>

#include "transport_codec.h"

namespace Transport {
  /// Eight-line RAM queue preserving message boundaries through partial USB writes.
  class UsbOutput {
   public:
    /// Queue a JSON line without LF; false means the line is oversized or the queue is full.
    bool push(std::string text) {
      if (text.size() > MAX_LINE || lines_.size() == USB_QUEUE_DEPTH) return false;
      text += '\n';
      lines_.push_back(std::move(text));
      return true;
    }

    /// Drain at most one line using available capacity. write returns the actual byte count.
    /// A zero/short write retains its cursor so the next call resumes the same message.
    template <class Write>
    void drain(size_t capacity, Write write) {
      if (lines_.empty() || !capacity) return;
      const auto& front = lines_.front();
      const auto amount = std::min(capacity, front.size() - cursor_);
      const auto sent   = write(front.data() + cursor_, amount);
      cursor_ += std::min<size_t>(sent, amount);
      if (cursor_ == front.size()) {
        lines_.pop_front();
        cursor_ = 0;
      }
    }

    /// Discard buffered output and any partial cursor on USB disconnection.
    void reset() {
      lines_.clear();
      cursor_ = 0;
    }

    /// Return whether every queued byte, including the terminating newline, has been written.
    bool empty() const { return lines_.empty(); }

   private:
    std::deque<std::string> lines_;
    size_t cursor_ = 0;
  };
}  // namespace Transport
