#include <Arduino.h>

#include "chair_constants.h"
#include "esp_private/system_internal.h"
#include "esp_timer.h"
#include "json_messages.h"
#include "link_config.h"
#include "radio_lifecycle.h"
#include "radio_transport.h"

namespace {
  Transport::RadioLifecycle lifecycle;
  uint64_t nextRetry = 0, recoveryAnnounced = 0;
  bool recoveryPending = false, timeoutReported = false;
}  // namespace

/// Initialize USB before asynchronous encrypted-radio startup.
void setup() {
  Transport::begin(Transport::GATEWAY_ROLE, LinkConfig::GATEWAY_MAC, LinkConfig::SENSOR_MAC);
}

/// Continue servicing USB even when the SDK lifecycle call fails to return.
void loop() {
  using SmartChair::RadioAction;
  Transport::poll();
  const uint64_t now = static_cast<uint64_t>(esp_timer_get_time()) /
                       SmartChair::Timing::MICROSECONDS_PER_MILLISECOND;
  if (const auto update = lifecycle.poll(now)) {
    const bool starting = update->action == RadioAction::START;
    if (update->timedOut && !timeoutReported) {
      timeoutReported = true;
      Transport::printJson(
          Transport::ErrorMessage{.reason = starting ? Transport::ErrorCode::RADIO_START_TIMEOUT
                                                     : Transport::ErrorCode::RADIO_STOP_TIMEOUT});
    }
    if (update->completed && !update->ok) {
      nextRetry = SmartChair::deadlineAfter(now, SmartChair::Timing::RADIO_RETRY_MS);
      Transport::printJson(
          Transport::ErrorMessage{.reason = starting ? Transport::ErrorCode::RADIO_RESUME_FAILED
                                                     : Transport::ErrorCode::RADIO_SUSPEND_FAILED});
    }
  }
  if (!recoveryPending && (lifecycle.stalled(now) || Transport::needsReboot())) {
    recoveryPending   = true;
    recoveryAnnounced = now;
    Transport::printJson(
        Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_RECOVERY_REBOOT});
  }
  if (recoveryPending && Transport::recoveryNoticeSent(recoveryAnnounced, now)) {
    // No durable gateway state: the sensor retries reliable frames after reconnection.
    // Bypass shutdown hooks that may wait on the stuck Wi-Fi task's locks.
    esp_restart_noos();
  }
  // An unconfigured transport already reported why; retrying it would only repeat errors.
  if (Transport::configured() && !lifecycle.busy() && !recoveryPending && !Transport::busy() &&
      now >= nextRetry && (!Transport::available() || Transport::needsRestart())) {
    const auto action = !Transport::stopped() ? RadioAction::STOP : RadioAction::START;
    timeoutReported   = false;
    if (!lifecycle.start(action, now)) {
      nextRetry = SmartChair::deadlineAfter(now, SmartChair::Timing::RADIO_RETRY_MS);
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_TASK_FAILED});
    }
  }
  delay(Transport::POLL_DELAY_MS);
}
