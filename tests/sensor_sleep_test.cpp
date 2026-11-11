#include <cassert>
#include <string>

#include "../sensor/src/main.cpp"
#include "../sensor/src/mpu6050.cpp"

namespace Fake {
  uint64_t now = 0;
  int seat = HIGH, sleeps = 0, stops = 0, sleepResult = ESP_OK, stopResult = ESP_OK;
  bool sending = false, closeOnTimerSetup = false;
  uint64_t wakeMask = 0, timerUs = 0;
  esp_sleep_wakeup_cause_t cause = ESP_SLEEP_WAKEUP_UNDEFINED;
  std::vector<std::vector<uint8_t>> sent;

  void tick(uint64_t time, int level) {
    now  = time;
    seat = level;
    loop();
  }

  void boot(esp_sleep_wakeup_cause_t reason = ESP_SLEEP_WAKEUP_UNDEFINED, bool clear = true) {
    now    = 0;
    seat   = HIGH;
    sleeps = stops = 0;
    sending = closeOnTimerSetup = false;
    sleepResult = stopResult = ESP_OK;
    cause                    = reason;
    storageReady             = false;
    nextImuProbe             = 0;
    if (clear) {
      Preferences::bytes.clear();
      retainedBoot = 0;
      retainedSeq  = 1;
    }
    Preferences::available = true;
    sent.clear();
    setup();
  }
}  // namespace Fake

FakeSerial Serial;

uint32_t millis() { return Fake::now; }

int64_t esp_timer_get_time() { return Fake::now * 1000; }

uint32_t esp_random() {
  static uint32_t n = 100;
  return ++n;
}

void delay(uint32_t) {}

void pinMode(uint8_t, int) {}

int digitalRead(uint8_t) { return Fake::seat; }

void FakeSerial::flush() {}

esp_sleep_wakeup_cause_t esp_sleep_get_wakeup_cause() { return Fake::cause; }

int esp_deep_sleep_enable_gpio_wakeup(uint64_t mask, esp_deepsleep_gpio_wake_up_mode_t mode) {
  assert(mode == ESP_GPIO_WAKEUP_GPIO_LOW);
  Fake::wakeMask = mask;
  return Fake::sleepResult;
}

int esp_sleep_enable_timer_wakeup(uint64_t timer) {
  Fake::timerUs = timer;
  if (Fake::closeOnTimerSetup) Fake::seat = LOW;
  return Fake::sleepResult;
}

void esp_deep_sleep_start() { ++Fake::sleeps; }

int esp_wifi_stop() {
  ++Fake::stops;
  return Fake::stopResult;
}

namespace Transport {
  void begin(const char*, const char*, const char*) {}

  void setReceiver(Receiver) {}

  bool available() { return true; }

  void poll() {}

  bool busy() { return Fake::sending; }

  bool send(const uint8_t* data, size_t size, uint32_t) {
    Fake::sent.emplace_back(data, data + size);
    Fake::sending = true;
    return true;
  }
}  // namespace Transport

int main() {
  using namespace SmartChair;
  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(49, HIGH);
  assert(Fake::sent.empty());
  Fake::tick(50, HIGH);
  assert(!Fake::sent.empty() && !Preferences::bytes.empty());
  Frame first;
  assert(decode(Fake::sent[0], first) && first.type == Type::STATE_SYNC);
  Fake::tick(2549, HIGH);
  assert(Fake::sleeps == 0);
  Fake::tick(2550, HIGH);
  assert(Fake::sleeps == 1 && Fake::stops == 1);
  assert(Fake::wakeMask == (1ULL << Pins::SEAT_SENSOR) && Fake::timerUs == 900000000ULL);
  auto bootId = first.id.boot;
  Fake::boot(ESP_SLEEP_WAKEUP_TIMER, false);
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  assert(engine->identity.boot == bootId && engine->durable.outbox.size() == 2);
  assert(engine->durable.outbox[0].id == first.id &&
         engine->durable.outbox[1].type == Type::HEARTBEAT);
  assert(engine->durable.outbox[1].id.seq != first.id.seq);
  assert(engine->durable.boot_generation == 1);
  Fake::boot(ESP_SLEEP_WAKEUP_GPIO, false);
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(engine->durable.outbox.back().type == Type::OCCUPIED);
  assert(engine->durable.outbox.back().id.boot == bootId);

  Fake::boot(ESP_SLEEP_WAKEUP_UNDEFINED, false);
  assert(engine->identity.boot != bootId && engine->durable.outbox[0].id == first.id);
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  assert(engine->durable.boot_generation == 2);
  assert(engine->durable.outbox.back().boot_generation == 2);

  Fake::boot();
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(engine->occupied && imuReady);
  Fake::tick(100, HIGH);
  Fake::tick(120, LOW);
  Fake::tick(200, LOW);
  assert(engine->occupied && Fake::sleeps == 0);
  Fake::tick(300, HIGH);
  Fake::tick(350, HIGH);
  assert(engine->operating == Operating::PREPARE_SLEEP);
  Fake::tick(400, LOW);
  Fake::tick(450, LOW);
  assert(engine->operating == Operating::OCCUPIED_IDLE && Fake::sleeps == 0);

  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::closeOnTimerSetup = true;
  Fake::tick(2550, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 0);  // Final physical-pin check prevents sleep.
  Fake::tick(2551, LOW);
  Fake::tick(2601, LOW);
  assert(engine->occupied);
  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::sleepResult = -1;
  Fake::tick(2550, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 0);
  Fake::sleepResult = ESP_OK;
  Fake::stopResult  = -1;
  Fake::tick(2552, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 1);
  Fake::boot();
  Preferences::available = false;
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::tick(10000, HIGH);
  assert(Fake::sleeps == 0 && Fake::sent.empty());

  Fake::boot();
  engine->durable.config.heartbeat_s = UINT32_MAX;
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::tick(2550, HIGH);
  assert(Fake::timerUs == 4294967295000000ULL);
}
