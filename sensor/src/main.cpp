#include <Arduino.h>
#include <Preferences.h>

#include <algorithm>
#include <memory>

#include "bench_diagnostics.h"
#include "chair_engine.h"
#include "config.h"
#include "esp_private/system_internal.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "json_messages.h"
#include "link_config.h"
#include "mpu6050.h"
#include "pins.h"
#include "radio_lifecycle.h"
#include "radio_transport.h"

static_assert(static_cast<uint8_t>(Pins::SEAT_SENSOR) == 5,
              "The C3 seat wake contact must use GPIO5");

namespace {
  using namespace SmartChair;

  Preferences preferences;
  std::unique_ptr<Engine> engine;
  RTC_DATA_ATTR uint64_t retainedBoot   = 0;
  RTC_DATA_ATTR uint32_t retainedSeq    = 1;
  RTC_DATA_ATTR bool retainedBatteryLow = false;
  // Survives software resets only; the magic rejects the random contents after power-on.
  RTC_NOINIT_ATTR uint32_t forcedRestartMagic;
  RTC_NOINIT_ATTR uint32_t forcedRestarts;
  Wake wakeReason           = Wake::cold_boot;
  bool storageReady         = false;
  bool storageResetRequired = false;
  bool imuReady             = false;
  uint64_t nextImuProbe     = 0;
  uint64_t nextBatteryRead  = 0;
  Transport::RadioLifecycle lifecycle;
  bool lifecycleTimeoutReported = false;
  bool recoveryPending          = false;
  uint64_t recoveryAnnounced    = 0;
  bool traceSeatSeen = false, traceRaw = false;

  uint64_t nowMs() { return uint64_t(esp_timer_get_time()) / Timing::MICROSECONDS_PER_MILLISECOND; }

  uint64_t newBootId(uint64_t previous) {
    uint64_t boot;
    do {
      boot = (static_cast<uint64_t>(esp_random()) << 32) | esp_random();
    } while (!boot || boot == previous);

    return boot;
  }

  bool save(const Durable& data) {
    if (!storageReady) return false;

    const auto bytes = encodeDurable(data);
    return preferences.putBytes(Config::Storage::STATE_KEY, bytes.data(), bytes.size()) ==
           bytes.size();
  }

  inline void startColdBoot() {
    retainedBoot = newBootId(retainedBoot);
    retainedSeq  = 1;
    // Unlike RTC memory, the cell may have been replaced or recharged while powered off.
    retainedBatteryLow = false;
    wakeReason         = Wake::cold_boot;
  }

  inline bool loadDurable(Durable& durable) {
    const auto size = preferences.getBytesLength(Config::Storage::STATE_KEY);
    if (!size || size > Limits::STORED_STATE_BYTES) return false;

    std::vector<uint8_t> bytes(size);
    return preferences.getBytes(Config::Storage::STATE_KEY, bytes.data(), size) == size &&
           decodeDurable(bytes, durable);
  }

  inline void initializeEngine(Durable durable) {
    Header identity;
    std::strncpy(identity.node.data(), LinkConfig::NODE_ID, identity.node.size() - 1);
    identity.boot = retainedBoot;
    identity.seq  = retainedSeq;
    engine        = std::make_unique<Engine>(identity, std::move(durable), save);
  }

  inline void initializeImu() {
    imuReady             = Mpu6050::begin();
    engine->imuAvailable = imuReady;
  }

  /// Average the divider's ADC readings into cell millivolts; no cell reads as unavailable.
  inline uint16_t readBatteryMv() {
    uint32_t total = 0;
    for (uint8_t i = 0; i < Battery::SAMPLES; ++i) total += analogReadMilliVolts(Pins::BATTERY_ADC);
    const uint32_t mv = total / Battery::SAMPLES * 2;
    if (mv < Battery::MIN_VALID_MV) return UINT16_MAX;
    return uint16_t(std::min<uint32_t>(mv, UINT16_MAX - 1));
  }

  inline void sampleBattery(uint64_t now) {
    engine->battery(readBatteryMv());
    nextBatteryRead = deadlineAfter(now, Battery::SAMPLE_INTERVAL_MS);
  }

  void receive(const uint8_t* bytes, size_t size) {
    Frame f;
    if (decode(std::span(bytes, size), f)) engine->receive(f, nowMs());
  }

  inline void probeImu(uint64_t now) {
    nextImuProbe = deadlineAfter(now, Mpu6050::RETRY_INTERVAL_MS);
    initializeImu();
  }

  inline void sampleImu(uint64_t now) {
    Sample sample;
    if (!imuReady || !Mpu6050::read(sample)) {
      imuReady = false;
      engine->sampleFailed(now);
      return;
    }

    engine->sample(sample, nowMs());
  }

  inline void sendFrame(const Frame& outgoing) {
    const auto bytes = encode(outgoing);
    if (!bytes.empty()) Transport::send(bytes.data(), bytes.size());
  }

  inline void sleepWhileVacant() {
    const uint64_t timerUs =
        milliseconds(engine->durable.config.heartbeat_s) * Timing::MICROSECONDS_PER_MILLISECOND;
    if (esp_deep_sleep_enable_gpio_wakeup(1ULL << Pins::SEAT_SENSOR, ESP_GPIO_WAKEUP_GPIO_LOW) !=
            ESP_OK ||
        esp_sleep_enable_timer_wakeup(timerUs) != ESP_OK) {
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::SLEEP_SETUP_FAILED});
      return;
    }
    // Recheck the physical contact after bounded delivery/persistence work.
    if (digitalRead(Pins::SEAT_SENSOR) == LOW) return;

    retainedBoot       = engine->identity.boot;
    retainedSeq        = engine->identity.seq;
    retainedBatteryLow = engine->batteryLow;

    esp_deep_sleep_start();
  }

  void collectLifecycle(uint64_t now) {
    const auto update = lifecycle.poll(now);
    if (!update) return;
    const bool starting = update->action == RadioAction::START;
    if (update->timedOut && !lifecycleTimeoutReported) {
      lifecycleTimeoutReported = true;
      // A worker that already finished reports whether it left Wi-Fi fully stopped.
      if (starting)
        engine->radioStartFailed(now, update->completed && !update->ok && Transport::stopped());
      Transport::printJson(
          Transport::ErrorMessage{.reason = starting ? Transport::ErrorCode::RADIO_START_TIMEOUT
                                                     : Transport::ErrorCode::RADIO_STOP_TIMEOUT});
    }
    if (!update->completed) return;
    if (starting) {
      if (!update->timedOut) {
        if (update->ok) {
          engine->radioReady(now);
          forcedRestarts = 0;  // The radio works again; the next stall starts a fresh count.
          Bench::trace("radio_ready", *engine, now, wakeReason);
        } else {
          engine->radioStartFailed(now, Transport::stopped());
        }
      }
    } else if (update->ok) {
      engine->radioStopped(now);
      Bench::trace("radio_stopped", *engine, now, wakeReason);
    } else {
      engine->radioStopFailed(now, Transport::available());
    }
    if (!update->ok)
      Transport::printJson(
          Transport::ErrorMessage{.reason = starting ? Transport::ErrorCode::RADIO_RESUME_FAILED
                                                     : Transport::ErrorCode::RADIO_SUSPEND_FAILED});
  }

  // Count consecutive forced restarts. Beyond the cap, keep waiting for a safe restart instead
  // of looping reboots that would repeatedly discard an unsaved transition.
  bool noteForcedRestart() {
    if (forcedRestartMagic != Limits::FORCED_RESTART_MAGIC) {
      forcedRestartMagic = Limits::FORCED_RESTART_MAGIC;
      forcedRestarts     = 0;
    }
    if (forcedRestarts >= Limits::MAX_FORCED_RESTARTS) return false;
    ++forcedRestarts;
    return true;
  }

  void recoverRadio(uint64_t now) {
    if (!recoveryPending && (lifecycle.stalled(now) || Transport::needsReboot())) {
      recoveryPending   = true;
      recoveryAnnounced = now;
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_RECOVERY_REBOOT});
    }
    if (!recoveryPending || !Transport::recoveryNoticeSent(recoveryAnnounced, now)) return;
    // The main loop is the only writer of chair state. Wait for every durable write and
    // debounce to finish, and check the pin again immediately before an emergency reset.
    // Storage that never recovers cannot preserve the transition anyway, so a bounded wait
    // ends in a restart rather than leaving the chair awake without a radio indefinitely.
    const bool safe =
        engine->canRestart(now) && (digitalRead(Pins::SEAT_SENSOR) == LOW) == engine->occupied;
    if (safe ||
        (now - recoveryAnnounced >= Timing::RADIO_RECOVERY_FORCE_MS && noteForcedRestart())) {
      // A stuck Wi-Fi task may also block esp_restart() shutdown hooks. The pinned IDF's
      // low-level reset bypasses those hooks and resets both CPUs and radio peripherals.
      esp_restart_noos();
    }
  }

  void reconcileRadio(uint64_t now) {
    collectLifecycle(now);
    if (lifecycle.busy() || recoveryPending || Transport::needsReboot()) return;
    const auto action = engine->radioAction(now, Transport::busy());
    if (action == RadioAction::NONE) return;
    if (action == RadioAction::START && Transport::available()) {
      engine->radioReady(now);
      return;
    }
    lifecycleTimeoutReported = false;
    Bench::trace(action == RadioAction::START ? "radio_start" : "radio_stop", *engine, now,
                 wakeReason);
    if (!lifecycle.start(action, now, Bench::START_DELAY_MS)) {
      if (action == RadioAction::START)
        engine->radioStartFailed(now, Transport::stopped());
      else
        engine->radioStopFailed(now, Transport::available());
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_TASK_FAILED});
      return;
    }
    collectLifecycle(nowMs());
  }
}  // namespace

/// Initialize storage, sensors and encrypted transport before starting cooperative updates.
void setup() {
  storageResetRequired     = false;
  lifecycleTimeoutReported = false;
  recoveryPending          = false;
  traceSeatSeen            = false;
  pinMode(Pins::SEAT_SENSOR, INPUT_PULLUP);

  const auto cause    = esp_sleep_get_wakeup_cause();
  const bool deepWake = cause == ESP_SLEEP_WAKEUP_GPIO || cause == ESP_SLEEP_WAKEUP_TIMER;
  wakeReason          = Wake::cold_boot;
  if (cause == ESP_SLEEP_WAKEUP_GPIO) wakeReason = Wake::seat;
  if (cause == ESP_SLEEP_WAKEUP_TIMER) wakeReason = Wake::timer;

  if (!deepWake || !retainedBoot || retainedSeq >= UINT32_MAX - Limits::SEQUENCE_RESERVE) {
    startColdBoot();
  }

  storageReady = preferences.begin(Config::Storage::NAMESPACE, false);
  Durable durable;
  if (storageReady && preferences.isKey(Config::Storage::STATE_KEY)) {
    storageReady         = loadDurable(durable);
    storageResetRequired = !storageReady;
  }

  initializeEngine(std::move(durable));
  engine->batteryLow = retainedBatteryLow;
  sampleBattery(nowMs());
  initializeImu();

  Transport::setReceiver(receive);
  Transport::begin(Transport::SENSOR_ROLE, LinkConfig::SENSOR_MAC, LinkConfig::GATEWAY_MAC);
  if (!storageReady)
    Transport::printJson(Transport::ErrorMessage{
        .reason = storageResetRequired ? Transport::ErrorCode::STORAGE_RESET_REQUIRED
                                       : Transport::ErrorCode::STORAGE_UNAVAILABLE_OR_CORRUPT});
}

/// Run one bounded hardware/engine iteration; vacant sleep is the only blocking power action.
void loop() {
  Transport::poll();
  const uint64_t now = nowMs();

  if (engine->identity.seq >= UINT32_MAX - Limits::SEQUENCE_RESERVE) {
    engine->rotateBoot(newBootId(engine->identity.boot), now);
  }

  const bool rawSeat     = digitalRead(Pins::SEAT_SENSOR) == LOW;
  const bool wasOccupied = engine->occupied;
  const bool wasMoving   = engine->motionActive();
  engine->seat(rawSeat, now, wakeReason);
  if (!traceSeatSeen || traceRaw != rawSeat || wasOccupied != engine->occupied) {
    Bench::trace("seat", *engine, now, wakeReason, rawSeat);
    traceSeatSeen = true;
    traceRaw      = rawSeat;
  }
  if (!imuReady && now >= nextImuProbe) probeImu(now);
  if (engine->sampleDue(now)) sampleImu(now);
  if (wasMoving != engine->motionActive())
    Bench::trace(engine->motionActive() ? "motion_start" : "motion_stop", *engine, nowMs(),
                 wakeReason);

  if (now >= nextBatteryRead) sampleBattery(now);
  engine->tick(nowMs(), Transport::busy());
  if (Transport::needsRestart()) engine->radioFault(nowMs());
  reconcileRadio(nowMs());
  recoverRadio(nowMs());

  Frame outgoing;
  if (!lifecycle.busy() && !recoveryPending && Transport::available() && !Transport::busy() &&
      engine->next(outgoing, nowMs())) {
    sendFrame(outgoing);
  }

  if (!recoveryPending && engine->operating == Operating::DEEP_SLEEP) sleepWhileVacant();

  delay(Transport::POLL_DELAY_MS);
}
