#include <algorithm>
#include <cassert>
#include <cstring>

#include "chair_engine.h"

using namespace SmartChair;

namespace {
  bool save(const Durable&) { return true; }

  Header id() {
    Header h;
    std::strcpy(h.node.data(), "chair-01");
    h.boot = 1;
    h.seq  = 1;
    return h;
  }

  /// A vacant chair that has finished its startup report.
  Engine started() {
    Engine e(id(), {}, save);
    e.imuAvailable = true;
    e.seat(false, 0);
    e.seat(false, 50);
    assert(e.operating != Operating::STARTING);
    return e;
  }

  size_t alerts(const Engine& e) {
    return std::ranges::count_if(e.durable.outbox,
                                 [](const Frame& f) { return f.type == Type::BATTERY_LOW; });
  }

  void defaults() {
    auto e = started();
    auto f = e.frame(Type::HEARTBEAT, 100);
    assert(f.common.battery == UINT16_MAX);
    assert(f.common.health & Health::BATTERY_UNAVAILABLE);
  }

  void reported() {
    auto e = started();
    e.battery(3910);
    auto f = e.frame(Type::HEARTBEAT, 100);
    assert(f.common.battery == 3910);
    assert(!(f.common.health & Health::BATTERY_UNAVAILABLE));
    assert(!(f.common.health & Health::IMU_UNAVAILABLE));
    e.imuAvailable = false;
    assert(e.frame(Type::HEARTBEAT, 100).common.health == Health::IMU_UNAVAILABLE);
  }

  void alertsOnce() {
    auto e = started();
    e.battery(3300);
    e.tick(100);
    assert(e.batteryLow && alerts(e) == 1);
    const auto alert = std::ranges::find_if(
        e.durable.outbox, [](const Frame& f) { return f.type == Type::BATTERY_LOW; });
    assert(alert->common.battery == 3300);
    e.tick(200);
    e.tick(300);
    assert(alerts(e) == 1);
  }

  void chatter() {
    auto e = started();
    for (uint16_t mv : {3390, 3410, 3390, 3410, 3390}) {
      e.battery(mv);
      e.tick(100);
    }
    assert(alerts(e) == 1);
  }

  void boundaries() {
    auto e = started();
    e.battery(Battery::LOW_MV);
    e.tick(100);
    assert(!e.batteryLow && alerts(e) == 0);
    e.battery(Battery::LOW_MV - 1);
    e.tick(200);
    assert(e.batteryLow && alerts(e) == 1);
    e.battery(Battery::REARM_MV - 1);
    e.tick(300);
    assert(e.batteryLow);
    e.battery(Battery::REARM_MV);
    e.tick(400);
    assert(!e.batteryLow);
    e.battery(Battery::LOW_MV - 1);
    e.tick(500);
    assert(e.batteryLow && alerts(e) == 2);
  }

  void unavailable() {
    auto e = started();
    e.battery(UINT16_MAX);
    e.tick(100);
    assert(!e.batteryLow && alerts(e) == 0);
    e.battery(3300);
    e.tick(200);
    e.battery(UINT16_MAX);
    e.tick(300);
    assert(e.batteryLow && alerts(e) == 1);
  }

  void restoredLatch() {
    Engine e(id(), {}, save);
    e.batteryLow = true;
    e.battery(3300);
    e.seat(false, 0);
    e.seat(false, 50);
    e.tick(100);
    assert(e.batteryLow && alerts(e) == 0);
  }

  void afterStartup() {
    Engine e(id(), {}, save);
    e.battery(3300);
    e.tick(0);
    e.seat(false, 0);
    e.tick(10);
    assert(alerts(e) == 0 && e.durable.outbox.empty());
    e.seat(false, 50);
    e.tick(60);
    assert(alerts(e) == 1);
    const auto& outbox = e.durable.outbox;
    assert(outbox.front().type == Type::STATE_SYNC);
    assert(outbox.back().type == Type::BATTERY_LOW);
    assert(outbox.front().id.seq < outbox.back().id.seq);
  }
}  // namespace

int main() {
  defaults();
  reported();
  alertsOnce();
  chatter();
  boundaries();
  unavailable();
  restoredLatch();
  afterStartup();
}
