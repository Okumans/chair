#include <cassert>
#include <string>

#include "../sensor/src/main.cpp"
#include "../sensor/src/mpu6050.cpp"

namespace Fake {
  uint64_t now = 0;
  int seat = HIGH, sleeps = 0, stops = 0, sleepResult = ESP_OK, stopResult = ESP_OK;
  bool sending = false, closeOnTimerSetup = false;
  bool radioOn = true, rebootRequired = false;
  int reboots  = 0;
  int suspends = 0, resumes = 0;
  uint64_t wakeMask = 0, timerUs = 0;
  esp_sleep_wakeup_cause_t cause = ESP_SLEEP_WAKEUP_UNDEFINED;
  std::vector<std::vector<uint8_t>> sent;

  void tick(uint64_t time, int level) {
    now     = time;
    seat    = level;
    sending = false;  // Complete the previous fake MAC transmission.
    loop();
  }

  void boot(esp_sleep_wakeup_cause_t reason = ESP_SLEEP_WAKEUP_UNDEFINED, bool clear = true) {
    now    = 0;
    seat   = HIGH;
    sleeps = stops = 0;
    sending = closeOnTimerSetup = false;
    radioOn                     = true;
    rebootRequired              = false;
    reboots                     = 0;
    suspends = resumes = 0;
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
    Wire.connected         = true;
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

void esp_restart_noos() { ++Fake::reboots; }

int esp_wifi_stop() {
  ++Fake::stops;
  return Fake::stopResult;
}

namespace Transport {
  void begin(const char*, const char*, const char*) { Fake::radioOn = false; }

  void setReceiver(Receiver) {}

  bool suspend(bool) {
    if (Fake::radioOn) {
      ++Fake::suspends;
      ++Fake::stops;
      if (Fake::stopResult != ESP_OK) return false;
    }
    Fake::radioOn = false;
    return true;
  }

  bool resume(bool) {
    if (!Fake::radioOn) ++Fake::resumes;
    Fake::radioOn = true;
    return true;
  }

  bool available() { return Fake::radioOn; }

  bool stopped() { return !Fake::radioOn; }

  bool needsRestart() { return false; }

  bool needsReboot() { return Fake::rebootRequired; }

  void setLifecycleBusy(bool) {}

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
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  Fake::sending = false;
  Fake::tick(5050, LOW);
  Fake::sending = false;
  Fake::tick(7550, LOW);
  assert(!Fake::radioOn && Fake::suspends == 1);
  Fake::tick(30049, LOW);
  assert(!Fake::radioOn);
  Fake::tick(30050, LOW);
  assert(Fake::radioOn && Fake::resumes == 2);

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
  assert(Fake::sleeps == 0 && Fake::stops == 1);  // Final physical-pin check prevents sleep.
  Fake::tick(2551, LOW);
  Fake::tick(2601, LOW);
  assert(engine->occupied);
  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::sleepResult = -1;
  Fake::tick(2550, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 1);
  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(50, HIGH);
  Fake::stopResult = -1;
  Fake::tick(2550, HIGH);
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

  Fake::boot();
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(imuReady && engine->imuAvailable);
  Wire.connected = false;
  Fake::tick(90, LOW);
  assert(!imuReady && !engine->imuAvailable);
  assert(!engine->sampleDue(129) && engine->sampleDue(130));
  Fake::tick(92, LOW);
  assert(!engine->sampleDue(129));  // Failed reads do not cause a retry on every loop.

  Fake::boot();
  FakeTask::defer = true;
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(FakeTask::pending && !Fake::radioOn);
  Fake::tick(90, LOW);
  assert(!engine->sampleDue(129));  // Sensing continues while radio startup is in progress.
  Fake::tick(2550, LOW);
  assert(engine->communication.state == Communication::SETTLING);
  FakeTask::run();
  FakeTask::defer = false;
  Fake::tick(2551, LOW);
  assert(engine->communication.state == Communication::SETTLING);
  Fake::tick(3550, LOW);
  Fake::tick(3551, LOW);
  assert(Fake::radioOn);

  Fake::boot();
  FakeTask::fail = true;
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(!Fake::radioOn && engine->communication.state == Communication::CLOSED);
  FakeTask::fail = false;
  Fake::tick(1049, LOW);
  assert(!Fake::radioOn);
  Fake::tick(1050, LOW);
  assert(Fake::radioOn && engine->communication.state == Communication::OPEN);

  Fake::boot();
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  Fake::tick(5050, LOW);
  FakeTask::defer = true;
  Fake::tick(7550, LOW);
  assert(FakeTask::pending && engine->communication.state == Communication::SETTLING);
  Fake::tick(10050, LOW);
  assert(engine->communication.state == Communication::SETTLING && Fake::radioOn);
  FakeTask::run();
  FakeTask::defer = false;
  Fake::tick(10051, LOW);
  assert(!Fake::radioOn && engine->communication.state == Communication::CLOSED);

  // A permanently stalled SDK task keeps sensing alive, then requests a safe full restart.
  Fake::boot();
  FakeTask::defer = true;
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  Fake::tick(15049, LOW);
  assert(Fake::reboots == 0 && engine->occupied);
  Fake::tick(15050, LOW);
  assert(recoveryPending && Fake::reboots == 0);
  Fake::tick(15070, HIGH);  // Do not reboot over a not-yet-persisted seat release.
  assert(Fake::reboots == 0);
  Preferences::available = false;
  Fake::tick(15120, HIGH);
  assert(engine->storageFailed && Fake::reboots == 0);
  Fake::tick(15370, HIGH);
  assert(Fake::reboots == 0 && Fake::sleeps == 0);
  Preferences::available = true;
  Fake::tick(15620, HIGH);
  assert(Fake::reboots == 1 && engine->canRestart(Fake::now));
  Durable persisted;
  assert(decodeDurable(Preferences::bytes, persisted));
  assert(persisted.outbox.back().type == Type::VACATED);
  // Fake a subsequent worker return so no host test leaves a dangling task across boots.
  FakeTask::run();
  lifecycle.poll(Fake::now);
  FakeTask::defer = false;

  Fake::boot();
  Fake::rebootRequired = true;
  Fake::tick(0, LOW);
  Fake::tick(20, LOW);
  assert(Fake::reboots == 0);  // Initial debounced state must be committed first.
  Fake::tick(50, LOW);
  assert(Fake::reboots == 1);

  Fake::boot();
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  Fake::rebootRequired = true;
  Fake::tick(100, LOW);
  Fake::tick(110, HIGH);
  Fake::tick(119, LOW);
  Fake::tick(120, LOW);
  Fake::tick(168, LOW);
  assert(Fake::reboots == 0);  // Returning to the prior state still needs a stable debounce.
  Fake::tick(169, LOW);
  assert(Fake::reboots == 1);
}
