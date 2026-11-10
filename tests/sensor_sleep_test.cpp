#include <cassert>
#include <string>

#include "../sensor/src/main.cpp"

namespace Fake {
  uint32_t now                   = 0;
  int seat                       = HIGH;
  bool sending                   = false;
  bool acceptSend                = true;
  bool closeDuringFlush          = false;
  int sleepSetupResult           = ESP_OK;
  int stopResult                 = ESP_OK;
  int sleeps                     = 0;
  int stops                      = 0;
  uint64_t wakeMask              = 0;
  uint64_t timerUs               = 0;
  esp_sleep_wakeup_cause_t cause = ESP_SLEEP_WAKEUP_UNDEFINED;
  std::string payload;

  void tick(uint32_t time, int level) {
    now  = time;
    seat = level;
    loop();
  }

  void boot(esp_sleep_wakeup_cause_t reason = ESP_SLEEP_WAKEUP_UNDEFINED) {
    now              = 0;
    seat             = HIGH;
    sending          = false;
    acceptSend       = true;
    closeDuringFlush = false;
    sleepSetupResult = ESP_OK;
    stopResult       = ESP_OK;
    sleeps           = 0;
    stops            = 0;
    cause            = reason;
    payload.clear();
    candidate = HIGH;
    stable    = -1;
    changedAt = lastReport = 0;
    reportQueued           = false;
    wakeReason             = "reset";
    setup();
  }
}  // namespace Fake

FakeSerial Serial;
uint32_t millis() { return Fake::now; }
void delay(uint32_t ms) { Fake::now += ms; }
void pinMode(uint8_t, int) {}
int digitalRead(uint8_t) { return Fake::seat; }
void FakeSerial::flush() {
  if (Fake::closeDuringFlush) Fake::seat = LOW;
}

esp_sleep_wakeup_cause_t esp_sleep_get_wakeup_cause() { return Fake::cause; }
int esp_deep_sleep_enable_gpio_wakeup(uint64_t mask, esp_deepsleep_gpio_wake_up_mode_t mode) {
  assert(mode == ESP_GPIO_WAKEUP_GPIO_LOW);
  Fake::wakeMask = mask;
  return Fake::sleepSetupResult;
}
int esp_sleep_enable_timer_wakeup(uint64_t us) {
  Fake::timerUs = us;
  return Fake::sleepSetupResult;
}
void esp_deep_sleep_start() { ++Fake::sleeps; }
int esp_wifi_stop() {
  ++Fake::stops;
  return Fake::stopResult;
}

namespace Transport {
  void begin(const char*, const char*, const char*) {}
  void poll() {}
  bool busy() { return Fake::sending; }
  bool send(const uint8_t* data, size_t size, uint32_t) {
    if (!Fake::acceptSend) return false;
    Fake::payload.assign(reinterpret_cast<const char*>(data), size);
    Fake::sending = true;
    return true;
  }
}  // namespace Transport

int main() {
  Fake::boot();
  Fake::tick(0, HIGH);
  Fake::tick(49, HIGH);
  assert(Fake::payload.empty());
  Fake::tick(50, HIGH);
  assert(Fake::payload.find("vacant") != std::string::npos);
  assert(Fake::sleeps == 0);  // Wait for the send callback before powering down.
  Fake::sending = false;
  Fake::tick(52, HIGH);
  assert(Fake::sleeps == 1 && Fake::stops == 1);
  assert(Fake::wakeMask == (1ULL << Pins::SEAT_SENSOR));
  assert(Fake::timerUs == uint64_t(Config::Timing::HEARTBEAT_INTERVAL_MS) * 1000);

  Fake::boot(ESP_SLEEP_WAKEUP_GPIO);
  Fake::tick(0, LOW);
  Fake::tick(50, LOW);
  assert(Fake::payload.find("occupied") != std::string::npos);
  assert(Fake::payload.find("\"wake_reason\":\"gpio\"") != std::string::npos);
  Fake::sending = false;
  Fake::tick(100, HIGH);  // Release bounce must not report vacancy or sleep.
  Fake::tick(120, LOW);
  Fake::tick(200, LOW);
  assert(Fake::sleeps == 0 && Fake::payload.find("occupied") != std::string::npos);
  Fake::tick(300, HIGH);
  Fake::tick(350, HIGH);
  assert(Fake::sleeps == 0 && Fake::payload.find("vacant") != std::string::npos);
  Fake::sending          = false;
  Fake::closeDuringFlush = true;
  Fake::tick(352, HIGH);  // Closure during preparation cancels sleep before radio stop.
  assert(Fake::sleeps == 0 && Fake::stops == 0);
  Fake::closeDuringFlush = false;
  Fake::tick(354, LOW);
  Fake::tick(404, LOW);
  assert(Fake::payload.find("occupied") != std::string::npos);

  Fake::sending              = false;
  const uint32_t heartbeatAt = 404 + Config::Timing::HEARTBEAT_INTERVAL_MS;
  Fake::tick(heartbeatAt - 1, LOW);
  assert(Fake::payload.find("\"uptime_ms\":404") != std::string::npos);
  Fake::tick(heartbeatAt, LOW);
  assert(Fake::payload.find("\"uptime_ms\":" + std::to_string(heartbeatAt)) != std::string::npos);
  assert(Fake::sleeps == 0);  // Occupied heartbeat keeps running without sleep.

  Fake::boot(ESP_SLEEP_WAKEUP_TIMER);
  Fake::tick(50, HIGH);
  assert(Fake::payload.find("\"wake_reason\":\"timer\"") != std::string::npos);
  Fake::tick(50 + Config::Timing::TX_WAIT_TIMEOUT_MS, HIGH);
  assert(Fake::sleeps == 1);  // A missing callback cannot keep the vacant node awake forever.

  Fake::boot();
  Fake::acceptSend = false;
  Fake::tick(50, HIGH);
  assert(Fake::sleeps == 0);  // Unconfigured/failed enqueue remains accessible over USB.

  Fake::boot();
  Fake::tick(50, HIGH);
  Fake::sending          = false;
  Fake::sleepSetupResult = -1;
  Fake::tick(52, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 0);

  Fake::boot();
  Fake::tick(50, HIGH);
  Fake::sending    = false;
  Fake::stopResult = -1;
  Fake::tick(52, HIGH);
  assert(Fake::sleeps == 0 && Fake::stops == 1);
}
