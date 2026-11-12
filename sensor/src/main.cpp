#include <Arduino.h>
#include <Preferences.h>

#include <algorithm>
#include <atomic>
#include <memory>

#include "chair_engine.h"
#include "config.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "json_messages.h"
#include "link_config.h"
#include "mpu6050.h"
#include "pins.h"
#include "radio_transport.h"

static_assert(static_cast<uint8_t>(Pins::SEAT_SENSOR) == 5,
              "The C3 seat wake contact must use GPIO5");

namespace {
  using namespace SmartChair;

  Preferences preferences;
  std::unique_ptr<Engine> engine;
  RTC_DATA_ATTR uint64_t retainedBoot = 0;
  RTC_DATA_ATTR uint32_t retainedSeq  = 1;
  Wake wakeReason                     = Wake::cold_boot;
  bool storageReady                   = false;
  bool storageResetRequired           = false;
  bool imuReady                       = false;
  uint64_t nextImuProbe               = 0;
  std::atomic<int> lifecycleResult{0};
  std::atomic<uint32_t> lifecycleElapsed{0};
  RadioAction lifecycleOperation = RadioAction::NONE;
  uint64_t lifecycleStarted      = 0;
  bool lifecycleTimedOut         = false;

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
    wakeReason   = Wake::cold_boot;
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

    retainedBoot = engine->identity.boot;
    retainedSeq  = engine->identity.seq;

    esp_deep_sleep_start();
  }

  void lifecycleWorker(void* argument) {
    const auto action = static_cast<RadioAction>(reinterpret_cast<uintptr_t>(argument));
    const bool ok =
        action == RadioAction::START ? Transport::resume(false) : Transport::suspend(false);
    lifecycleElapsed.store(
        static_cast<uint32_t>(std::min<uint64_t>(UINT32_MAX, nowMs() - lifecycleStarted)));
    lifecycleResult.store(ok ? 1 : -1);
    vTaskDelete(nullptr);
  }

  void collectLifecycle(uint64_t now) {
    if (lifecycleOperation == RadioAction::NONE) return;
    const int result = lifecycleResult.exchange(0);
    if (!result) {
      const auto timeout = lifecycleOperation == RadioAction::START ? Timing::RADIO_START_TIMEOUT_MS
                                                                    : Timing::RADIO_STOP_TIMEOUT_MS;
      if (!lifecycleTimedOut && now - lifecycleStarted >= timeout) {
        lifecycleTimedOut = true;
        if (lifecycleOperation == RadioAction::START) engine->radioStartFailed(now, false);
        Transport::printJson(
            Transport::ErrorMessage{.reason = lifecycleOperation == RadioAction::START
                                                  ? Transport::ErrorCode::RADIO_START_TIMEOUT
                                                  : Transport::ErrorCode::RADIO_STOP_TIMEOUT});
      }
      return;
    }
    const auto completed = lifecycleOperation;
    lifecycleOperation   = RadioAction::NONE;
    Transport::setLifecycleBusy(false);
    if (completed == RadioAction::START && !lifecycleTimedOut &&
        lifecycleElapsed.load() >= Timing::RADIO_START_TIMEOUT_MS) {
      lifecycleTimedOut = true;
      engine->radioStartFailed(now, result < 0 && Transport::stopped());
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_START_TIMEOUT});
    }
    if (completed == RadioAction::START) {
      if (!lifecycleTimedOut) {
        if (result > 0)
          engine->radioReady(now);
        else
          engine->radioStartFailed(now, Transport::stopped());
      }
    } else if (result > 0) {
      engine->radioStopped(now);
    } else {
      engine->radioStopFailed(now, Transport::available());
    }
    if (result < 0)
      Transport::printJson(Transport::ErrorMessage{
          .reason = completed == RadioAction::START ? Transport::ErrorCode::RADIO_RESUME_FAILED
                                                    : Transport::ErrorCode::RADIO_SUSPEND_FAILED});
  }

  void reconcileRadio(uint64_t now) {
    collectLifecycle(now);
    if (lifecycleOperation != RadioAction::NONE) return;
    const auto action = engine->radioAction(now, Transport::busy());
    if (action == RadioAction::NONE) return;
    if (action == RadioAction::START && Transport::available()) {
      engine->radioReady(now);
      return;
    }
    lifecycleOperation = action;
    lifecycleStarted   = now;
    lifecycleTimedOut  = false;
    lifecycleResult.store(0);
    lifecycleElapsed.store(0);
    Transport::setLifecycleBusy(true);
    if (xTaskCreate(lifecycleWorker, "chair-radio", 6144,
                    reinterpret_cast<void*>(static_cast<uintptr_t>(action)), 1,
                    nullptr) != pdPASS) {
      lifecycleOperation = RadioAction::NONE;
      Transport::setLifecycleBusy(false);
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
  storageResetRequired = false;
  lifecycleResult.store(0);
  lifecycleOperation = RadioAction::NONE;
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

  engine->seat(digitalRead(Pins::SEAT_SENSOR) == LOW, now, wakeReason);
  if (!imuReady && now >= nextImuProbe) probeImu(now);
  if (engine->sampleDue(now)) sampleImu(now);

  engine->tick(nowMs(), Transport::busy());
  if (Transport::needsRestart()) engine->radioFault(nowMs());
  reconcileRadio(nowMs());

  Frame outgoing;
  if (lifecycleOperation == RadioAction::NONE && Transport::available() && !Transport::busy() &&
      engine->next(outgoing, nowMs())) {
    sendFrame(outgoing);
  }

  if (engine->operating == Operating::DEEP_SLEEP) sleepWhileVacant();

  delay(Transport::POLL_DELAY_MS);
}
