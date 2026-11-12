#include <Arduino.h>
#include <Preferences.h>

#include <memory>

#include "chair_engine.h"
#include "config.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "esp_wifi.h"
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
  bool imuReady                       = false;
  uint64_t nextImuProbe               = 0;

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
    nextImuProbe = now + Mpu6050::RETRY_INTERVAL_MS;
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

    if (esp_wifi_stop() == ESP_OK)
      esp_deep_sleep_start();
    else
      Transport::printJson(
          Transport::ErrorMessage{.reason = Transport::ErrorCode::RADIO_STOP_FAILED});
  }
}  // namespace

/// Initialize storage, sensors and encrypted transport before starting cooperative updates.
void setup() {
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
    storageReady = loadDurable(durable);
  }

  initializeEngine(std::move(durable));
  initializeImu();

  Transport::setReceiver(receive);
  Transport::begin(Transport::SENSOR_ROLE, LinkConfig::SENSOR_MAC, LinkConfig::GATEWAY_MAC);
  if (!storageReady)
    Transport::printJson(
        Transport::ErrorMessage{.reason = Transport::ErrorCode::STORAGE_UNAVAILABLE_OR_CORRUPT});
}

/// Run one bounded hardware/engine iteration; vacant sleep is the only blocking power action.
void loop() {
  Transport::poll();
  const uint64_t now = nowMs();

  if (engine->identity.seq >= UINT32_MAX - Limits::SEQUENCE_RESERVE) {
    engine->rotateBoot(newBootId(engine->identity.boot), now);
  }

  engine->seat(digitalRead(Pins::SEAT_SENSOR) == LOW, now, wakeReason);
  if (Transport::available()) engine->radioReady(now);

  if (!imuReady && now >= nextImuProbe) probeImu(now);
  if (engine->sampleDue(now)) sampleImu(now);

  engine->tick(nowMs(), Transport::busy());

  Frame outgoing;
  if (Transport::available() && !Transport::busy() && engine->next(outgoing, nowMs())) {
    sendFrame(outgoing);
  }

  if (engine->operating == Operating::DEEP_SLEEP) sleepWhileVacant();

  delay(Transport::POLL_DELAY_MS);
}
