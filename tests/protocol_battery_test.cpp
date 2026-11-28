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

  /// Acknowledge the pending BATTERY_LOW as the Pi would.
  void ackAlert(Engine& e, uint64_t now) {
    const auto alert = std::ranges::find_if(
        e.durable.outbox, [](const Frame& f) { return f.type == Type::BATTERY_LOW; });
    assert(alert != e.durable.outbox.end());
    Frame ack;
    ack.id       = id();
    ack.type     = Type::EVENT_ACK;
    ack.ack_boot = alert->id.boot;
    ack.ack_seq  = alert->id.seq;
    e.receive(ack, now);
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
    assert(!e.batteryLow && alerts(e) == 1);  // Pending until the Pi acknowledges it.
    const auto alert = std::ranges::find_if(
        e.durable.outbox, [](const Frame& f) { return f.type == Type::BATTERY_LOW; });
    assert(alert->common.battery == 3300);
    e.tick(200);
    e.tick(300);
    assert(alerts(e) == 1);
    ackAlert(e, 400);
    assert(e.batteryLow && alerts(e) == 0);
    e.tick(500);
    assert(alerts(e) == 0);
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
    assert(alerts(e) == 1);
    ackAlert(e, 250);
    assert(e.batteryLow);
    e.battery(Battery::REARM_MV - 1);
    e.tick(300);
    assert(e.batteryLow);
    e.battery(Battery::REARM_MV);
    e.tick(400);
    assert(!e.batteryLow);
    e.battery(Battery::LOW_MV - 1);
    e.tick(500);
    assert(alerts(e) == 1);
  }

  void unavailable() {
    auto e = started();
    e.battery(UINT16_MAX);
    e.tick(100);
    assert(!e.batteryLow && alerts(e) == 0);
    e.battery(3300);
    e.tick(200);
    ackAlert(e, 250);
    e.battery(UINT16_MAX);
    e.tick(300);
    assert(e.batteryLow && alerts(e) == 0);
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

  /// A long hub outage overflows the outbox; the dropped alert must be raised again.
  void survivesOverflow() {
    Engine e(id(), {}, save);
    e.imuAvailable = true;
    e.seat(true, 0);
    e.seat(true, 50);
    e.battery(3300);
    e.tick(100);
    assert(alerts(e) == 1);
    const uint64_t heartbeat = uint64_t(e.durable.config.heartbeat_s) * 1000;
    uint64_t now             = 100;
    for (size_t i = 0; i < 2 * Limits::OUTBOX_CAPACITY; ++i) {
      now += heartbeat;
      e.tick(now);
      assert(e.durable.outbox.size() < Limits::OUTBOX_CAPACITY);
    }
    assert(!e.batteryLow && alerts(e) == 1);
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
  survivesOverflow();
  afterStartup();
}
