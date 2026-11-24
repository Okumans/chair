#include <cassert>

#include "../gateway/src/main.cpp"

FakeSerial Serial;

namespace Fake {
  uint64_t now = 0;
  bool radioOn = false, rebootRequired = false, restartRequired = false, configured = true;
  bool drainUsb = true;
  int polls = 0, reboots = 0;
}  // namespace Fake

int64_t esp_timer_get_time() { return Fake::now * 1000; }

void delay(uint32_t) {}

void esp_restart_noos() { ++Fake::reboots; }

namespace Transport {
  void begin(const char*, const char*, const char*) {}

  void poll() {
    ++Fake::polls;
    if (Fake::drainUsb) usbOutput.reset();
  }

  bool configured() { return Fake::configured; }

  bool available() { return Fake::radioOn; }

  bool stopped() { return !Fake::radioOn; }

  bool busy() { return false; }

  bool needsRestart() { return Fake::restartRequired; }

  bool needsReboot() { return Fake::rebootRequired; }

  void setLifecycleBusy(bool) {}

  bool resume(bool) { return Fake::radioOn = true; }

  bool suspend(bool) {
    Fake::radioOn         = false;
    Fake::restartRequired = false;
    return true;
  }
}  // namespace Transport

int main() {
  setup();
  FakeTask::fail = true;
  loop();
  assert(!lifecycle.busy() && !Fake::radioOn);
  FakeTask::fail = false;
  Fake::now      = 999;
  loop();
  assert(!Fake::radioOn);
  Fake::now = 1000;
  loop();
  loop();
  assert(Fake::radioOn && !lifecycle.busy());
  Fake::restartRequired = true;
  loop();
  loop();
  loop();
  assert(Fake::radioOn && !Fake::restartRequired);

  // A stop that never returns must not prevent USB polling or trigger a competing worker.
  FakeTask::defer       = true;
  Fake::restartRequired = true;
  loop();
  assert(lifecycle.busy() && FakeTask::pending);
  Fake::now = 3500;
  loop();
  const int before = Fake::polls;
  Fake::now        = 15999;
  loop();
  assert(Fake::polls == before + 1 && !Fake::reboots);
  Fake::now = 16000;
  loop();
  assert(recoveryPending && !Fake::reboots);
  Fake::now = 16020;
  loop();
  assert(Fake::reboots == 1);
  FakeTask::run();
  lifecycle.poll(Fake::now);

  // A notice still queued behind a stalled USB host delays the reset only boundedly.
  Fake::drainUsb       = false;
  recoveryPending      = false;
  Fake::reboots        = 0;
  Fake::rebootRequired = true;
  Fake::now            = 20000;
  loop();
  Fake::now = 20249;
  loop();
  assert(recoveryPending && !Fake::reboots);
  Fake::now = 20250;
  loop();
  assert(Fake::reboots == 1);
  Fake::drainUsb       = true;
  Fake::rebootRequired = false;
  recoveryPending      = false;
  Transport::usbOutput.reset();

  // An unconfigured transport is never retried, so it cannot repeat resume failures.
  Fake::configured = false;
  Fake::radioOn    = false;
  Fake::now        = 30000;
  loop();
  assert(!lifecycle.busy() && !Fake::radioOn);
}
