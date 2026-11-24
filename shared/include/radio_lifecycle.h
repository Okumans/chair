#pragma once

#include <Arduino.h>

#include <algorithm>
#include <atomic>
#include <optional>

#include "chair_engine.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "json_messages.h"
#include "radio_transport.h"

namespace Transport {
  /// Give the recovery notice time to leave the USB queue, without ever waiting unbounded.
  inline bool recoveryNoticeSent(uint64_t announced, uint64_t now) {
    const uint64_t elapsed = now - announced;
    return elapsed >= SmartChair::Timing::RADIO_RECOVERY_FLUSH_MS ||
           (elapsed >= SmartChair::Timing::RADIO_RECOVERY_NOTICE_MS && usbOutput.empty());
  }

  /// One SDK worker at a time. A timeout never deletes a task that may own SDK locks.
  class RadioLifecycle {
   public:
    struct Update {
      SmartChair::RadioAction action;
      bool completed;
      bool ok;
      bool timedOut;
    };

    bool busy() const { return action_ != SmartChair::RadioAction::NONE; }

    bool stalled(uint64_t now) const {
      return busy() && now - started_ >= SmartChair::Timing::RADIO_RECOVERY_TIMEOUT_MS;
    }

    bool start(SmartChair::RadioAction action, uint64_t now, uint32_t startDelayMs = 0) {
      if (busy() || action == SmartChair::RadioAction::NONE) return false;
      action_       = action;
      started_      = now;
      startDelayMs_ = startDelayMs;
      timedOut_     = false;
      result_.store(0);
      elapsed_.store(0);
      setLifecycleBusy(true);
      if (xTaskCreate(worker, "chair-radio", 6144, this, 1, nullptr) == pdPASS) return true;
      action_ = SmartChair::RadioAction::NONE;
      setLifecycleBusy(false);
      return false;
    }

    std::optional<Update> poll(uint64_t now) {
      if (!busy()) return std::nullopt;
      const auto timeout = action_ == SmartChair::RadioAction::START
                               ? SmartChair::Timing::RADIO_START_TIMEOUT_MS
                               : SmartChair::Timing::RADIO_STOP_TIMEOUT_MS;
      const int result   = result_.exchange(0);
      if (result) {
        const Update update{action_, true, result > 0, timedOut_ || elapsed_.load() >= timeout};
        action_ = SmartChair::RadioAction::NONE;
        setLifecycleBusy(false);
        return update;
      }
      if (!timedOut_ && now - started_ >= timeout) {
        timedOut_ = true;
        return Update{action_, false, false, true};
      }
      return std::nullopt;
    }

   private:
    SmartChair::RadioAction action_ = SmartChair::RadioAction::NONE;
    uint64_t started_               = 0;
    uint32_t startDelayMs_          = 0;
    bool timedOut_                  = false;
    std::atomic<int> result_{0};
    std::atomic<uint32_t> elapsed_{0};

    static void worker(void* argument) {
      auto& lifecycle     = *static_cast<RadioLifecycle*>(argument);
      const bool starting = lifecycle.action_ == SmartChair::RadioAction::START;
      if (starting && lifecycle.startDelayMs_) delay(lifecycle.startDelayMs_);
      const bool ok = starting ? resume(false) : suspend(false);
      const uint64_t now =
          uint64_t(esp_timer_get_time()) / SmartChair::Timing::MICROSECONDS_PER_MILLISECOND;
      lifecycle.elapsed_.store(
          static_cast<uint32_t>(std::min<uint64_t>(UINT32_MAX, now - lifecycle.started_)));
      // No access to this object after publishing completion; the loop may start another worker.
      lifecycle.result_.store(ok ? 1 : -1);
      vTaskDelete(nullptr);
    }
  };
}  // namespace Transport
