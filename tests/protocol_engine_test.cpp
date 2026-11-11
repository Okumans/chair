#include <algorithm>
#include <cassert>

#include "chair_engine.h"
#include "usb_output.h"

using namespace SmartChair;

namespace {
  Durable disk;
  bool storage   = true;
  unsigned saves = 0;

  bool save(const Durable& d) {
    ++saves;
    if (!storage) return false;
    disk = d;
    return true;
  }

  Header id(uint64_t boot = 0x0807060504030201ULL) {
    Header h;
    std::strcpy(h.node.data(), "chair-01");
    h.boot = boot;
    h.seq  = 1;
    return h;
  }

  Engine engine(bool occupied = true) {
    Engine e(id(), {}, save);
    e.imuAvailable = true;
    e.seat(occupied, 0);
    e.seat(occupied, 50);
    e.radioReady(50);
    return e;
  }

  Frame take(Engine& e, uint64_t now, Type type) {
    Frame f;
    for (unsigned i = 0; i < 200 && e.next(f, now); ++i)
      if (f.type == type) return f;
    assert(false);
    return {};
  }

  Frame ack(const Frame& f) {
    Frame a;
    a.type     = Type::EVENT_ACK;
    a.id       = id(9);
    a.ack_boot = f.id.boot;
    a.ack_seq  = f.id.seq;
    return a;
  }

  Frame config(uint32_t revision, uint8_t uuid = 1) {
    Frame f;
    f.type            = Type::CONFIG_SET;
    f.id              = id(9);
    f.command[0]      = uuid;
    f.config.revision = revision;
    return f;
  }

  Sample active{0, 18000, 100, 2000, 2500, -32768, 32767};
  Sample quiet{0, 0, 0, 16384, 0, 0, 0};

  void motion(Engine& e) {
    for (uint64_t now : {100, 140, 180, 220}) e.sample(active, now);
    assert(e.operating == Operating::MOVING);
  }
}  // namespace

int main() {
  Frame f;
  Window w;
  w.open(1000);
  w.tick(5000, false);
  assert(w.state == Communication::OPENING && !w.allowsSleep());
  w.radioReady(5000);
  assert(w.remaining(5100) == 900);
  w.tick(5999, false);
  assert(w.state == Communication::OPEN);
  w.tick(6000, true);
  assert(w.state == Communication::SETTLING);
  w.extend(6100, 2000);
  assert(w.state == Communication::OPEN && w.remaining(6100) == 2000);
  w.tick(8100, true);
  w.tick(10600, true);
  assert(w.allowsSleep());
  w.open();
  w.radioReady(0);
  assert(w.remaining(100000) == UINT32_MAX);

  auto e = engine();
  assert(e.occupied && e.operating == Operating::OCCUPIED_IDLE && !e.canOccupiedSleep());
  assert(e.sampleDue(50));
  e.sample(quiet, 50);
  assert(!e.sampleDue(89) && e.sampleDue(90));
  auto sync  = take(e, 50, Type::STATE_SYNC);
  auto ready = take(e, 50, Type::READY);
  assert(ready.reason == Reason::seat_occupied && ready.listen == UINT32_MAX);
  assert(e.durable.outbox.size() == 1);
  assert(!e.next(f, 249));
  assert(!e.next(f, 299));
  assert(e.next(f, 300) && f.id == sync.id);
  assert(!e.next(f, 799));
  assert(e.next(f, 800));
  assert(e.next(f, 1800));
  assert(!e.next(f, 2549));
  assert(e.next(f, 2550));
  auto wrong = ack(sync);
  ++wrong.ack_seq;
  e.receive(wrong, 2551);
  assert(e.durable.outbox.size() == 1);
  storage = false;
  e.receive(ack(sync), 2552);
  assert(e.durable.outbox.size() == 1);
  storage = true;
  e.receive(ack(sync), 2553);
  assert(e.durable.outbox.empty());
  e.receive(ack(sync), 2554);
  assert(e.durable.outbox.empty());

  e = engine();
  motion(e);
  ready = take(e, 220, Type::READY);  // Seat availability precedes movement availability.
  assert(ready.reason == Reason::seat_occupied);
  ready = take(e, 220, Type::READY);
  assert(ready.reason == Reason::movement_started);
  for (uint64_t now : {260, 300, 340, 380}) e.sample(quiet, now);
  auto batch = take(e, 380, Type::MOVEMENT_BATCH);
  assert(batch.end == End::stopped && batch.samples.size() == 8 &&
         batch.samples[0].ax == active.ax);
  assert(batch.samples.back().offset == 280);
  assert(e.operating == Operating::OCCUPIED_IDLE);
  for (uint64_t now = 420; now < 5000; now += 40) e.sample(quiet, now);
  assert(!e.next(f, 5000) || f.type != Type::MOVEMENT_BATCH);

  e                                 = engine();
  e.durable.config.movement_batch_s = UINT32_MAX;
  motion(e);
  for (uint64_t now = 260; now <= 3060; now += 40) e.sample(active, now);
  batch = take(e, 3060, Type::MOVEMENT_BATCH);
  assert(batch.samples.size() == 75 && batch.end == End::capacity);
  assert(encode(batch).size() == 1108);
  e.seat(false, 3100);
  e.seat(false, 3150);
  batch = take(e, 3150, Type::MOVEMENT_BATCH);
  assert(batch.samples.empty() && batch.end == End::vacant);
  assert(e.operating == Operating::PREPARE_SLEEP);
  e.tick(5649, true);
  assert(e.operating == Operating::PREPARE_SLEEP);
  e.tick(5650, true);
  assert(e.operating == Operating::DEEP_SLEEP);
  e.seat(true, 5651);
  e.tick(5651);
  assert(e.operating == Operating::DEEP_SLEEP);
  e.seat(true, 5701);
  assert(e.operating == Operating::OCCUPIED_IDLE);

  // A stop at the periodic boundary wins and includes all debounce samples.
  e                                 = engine();
  e.durable.config.movement_batch_s = 1;
  motion(e);
  for (uint64_t now = 260; now < 980; now += 40) e.sample(active, now);
  for (uint64_t now : {980, 1020, 1060, 1100}) e.sample(quiet, now);
  batch = take(e, 1100, Type::MOVEMENT_BATCH);
  assert(batch.end == End::stopped && batch.duration == 1000);

  // Quiet sampling while the future radio window is closed must keep it closed.
  e                     = engine();
  e.communication.state = Communication::CLOSED;
  e.sample(quiet, 100);
  assert(e.communication.state == Communication::CLOSED);
  e.occupied_sleep_ms = 40;
  assert(e.canOccupiedSleep());
  e.communication.open(100);
  assert(!e.canOccupiedSleep());

  e       = engine();
  auto c  = config(1);
  storage = false;
  e.receive(c, 100);
  assert(e.durable.config.revision == 0);
  storage = true;
  e.receive(c, 101);
  assert(e.durable.config.revision == 1 && e.durable.commands.size() == 1);
  auto result = take(e, 101, Type::COMMAND_RESULT);
  assert(result.status == Status::applied);
  e.receive(c, 102);
  assert(e.durable.commands.size() == 1 && e.durable.outbox.size() == 2);
  auto savedBytes = encodeDurable(e.durable);
  Durable restored;
  assert(decodeDurable(savedBytes, restored));
  Engine rebooted(id(10), restored, save);
  rebooted.receive(c, 200);
  assert(rebooted.durable.outbox.size() == 2);
  c.config.heartbeat_s = 1;
  rebooted.receive(c, 201);
  assert(rebooted.durable.config.heartbeat_s == 900 &&
         rebooted.durable.outbox.back().status == Status::command_conflict);
  auto corrupt = savedBytes;
  corrupt.pop_back();
  assert(!decodeDurable(corrupt, restored));

  e = engine();
  for (unsigned i = 1; i <= 31; ++i) e.receive(config(i, i), 100 + i);
  assert(e.durable.outbox.size() == 32 && e.durable.commands.size() == 31);
  const auto revision = e.durable.config.revision;
  e.receive(config(32, 32), 200);
  assert(e.durable.config.revision == revision);
  e.seat(false, 300);
  e.seat(false, 350);
  assert(e.durable.outbox.size() == 32 && e.durable.outbox.back().type == Type::STATE_SYNC);
  assert(!e.durable.outbox.back().common.occupied);
  for (unsigned i = 0; i < 31; ++i) assert(e.durable.outbox[i].type == Type::COMMAND_RESULT);

  e       = engine();
  storage = false;
  e.seat(false, 100);
  e.seat(false, 150);
  e.tick(10000, false);
  assert(e.operating == Operating::PREPARE_SLEEP && e.storageFailed);
  storage = true;
  e.tick(10250, false);
  assert(!e.storageFailed && e.operating == Operating::DEEP_SLEEP);
  assert(e.durable.outbox.back().type == Type::STATE_SYNC);

  // Arbitrary short writes retain their cursor and never mix JSON messages.
  Transport::UsbOutput output;
  assert(output.push("first") && output.push("second"));
  std::string written;
  output.drain(100, [&](const char*, size_t) { return 0; });
  while (!output.empty())
    output.drain(3, [&](const char* data, size_t n) {
      const size_t sent = std::min<size_t>(2, n);
      written.append(data, sent);
      return sent;
    });
  assert(written == "first\nsecond\n");
  assert(!output.push(std::string(8193, 'x')));
  for (unsigned i = 0; i < 8; ++i) assert(output.push("x"));
  assert(!output.push("overflow"));
  output.reset();
  assert(output.empty());
  // Extending a radio that is still opening must preserve the longer requested window.
  w.open(1000);
  w.extend(100, 100);
  w.radioReady(500);
  assert(w.remaining(500) == 1000);

  e = engine();
  take(e, 50, Type::STATE_SYNC);
  take(e, 50, Type::READY);
  e.communication.state = Communication::CLOSED;
  e.occupied_sleep_ms   = 40;
  e.sample(quiet, 100);
  e.tick(101);
  assert(e.operating == Operating::OCCUPIED_LIGHT_SLEEP);
  e.tick(140);
  assert(e.operating == Operating::SAMPLE_CHECK && e.communication.allowsSleep());
  e.movement_window_ms = 2000;
  for (uint64_t now : {180, 220, 260, 300}) e.sample(active, now);
  assert(e.communication.state == Communication::OPENING && !e.canOccupiedSleep());
  e.radioReady(500);
  ready = take(e, 600, Type::READY);
  assert(ready.reason == Reason::movement_started && ready.listen == 1900);

  // Disabling sensing while vacant must preserve the pending deep-sleep transition.
  e                         = engine(false);
  c                         = config(1);
  c.config.movement_enabled = false;
  e.receive(c, 100);
  assert(e.operating == Operating::PREPARE_SLEEP);
  e.tick(2550, true);
  assert(e.operating == Operating::DEEP_SLEEP);

  // Acknowledged UUIDs remain idempotent after a reset and a later config revision.
  e = engine();
  c = config(1);
  e.receive(c, 100);
  result = take(e, 100, Type::COMMAND_RESULT);
  e.receive(ack(result), 101);
  e.receive(config(2, 2), 102);
  restored = e.durable;
  Engine anotherBoot(id(20), restored, save);
  anotherBoot.receive(c, 103);
  assert(anotherBoot.durable.config.revision == 2);
  assert(anotherBoot.durable.outbox.back().status == Status::applied);
  assert(anotherBoot.durable.outbox.back().command == c.command);

  e                            = engine();
  e.durable.config.heartbeat_s = 1;
  for (uint64_t now = 1050; now < 100000; now += 1000) e.tick(now);
  assert(e.durable.outbox.size() <= 32);
  assert(std::any_of(e.durable.outbox.begin(), e.durable.outbox.end(),
                     [](const auto& frame) { return frame.type == Type::STATE_SYNC; }));
  const auto originalIdentity = e.durable.outbox.front().id;
  e.rotateBoot(99, 100001);
  assert(e.durable.outbox.front().id == originalIdentity);
  assert(e.durable.outbox.back().type == Type::STATE_SYNC);
  assert(e.durable.outbox.back().id.boot == 99 && e.durable.outbox.back().id.seq == 1);
  assert(e.identity.seq == 2);
  assert(e.durable.boot_generation == 2 && e.durable.outbox.back().boot_generation == 2);
  e                     = engine();
  e.communication.state = Communication::CLOSED;
  e.tick(900050);
  assert(e.communication.state == Communication::OPENING);
  assert(e.durable.outbox.back().type == Type::HEARTBEAT);
  // A delayed ACK must not let later acknowledged commands evict the original UUID record.
  e = engine();
  c = config(1);
  e.receive(c, 100);
  for (uint32_t revision = 2; revision <= 40; ++revision) {
    e.receive(config(revision, revision), revision * 100);
    result = e.durable.outbox.back();
    assert(result.type == Type::COMMAND_RESULT && result.command != c.command);
    e.receive(ack(result), revision * 100 + 1);
  }
  const auto pendingBeforeReplay = e.durable.outbox.size();
  e.receive(c, 4100);
  assert(e.durable.outbox.size() == pendingBeforeReplay);
  assert(e.durable.commands.size() == 32);
  assert(e.durable.config.revision == 40);
  // Legacy NVS format upgrades without dropping outstanding event identities.
  auto legacyState = e.durable;
  std::erase_if(legacyState.outbox,
                [](const auto& frame) { return frame.type == Type::STATE_SYNC; });
  auto legacyStore = encodeDurable(legacyState);
  legacyStore[0]   = 0x53;
  legacyStore[1]   = 0x43;
  legacyStore[2]   = 0x48;
  legacyStore[3]   = 0x31;
  legacyStore.erase(legacyStore.begin() + 4, legacyStore.begin() + 12);
  assert(decodeDurable(legacyStore, restored) && restored.boot_generation == 0);
  Engine upgraded(id(100), restored, save);
  upgraded.seat(true, 0, Wake::seat);
  upgraded.seat(true, 50, Wake::seat);
  assert(upgraded.durable.boot_generation == 1);
  assert(upgraded.durable.outbox.back().type == Type::STATE_SYNC);

  Durable exhausted;
  exhausted.boot_generation = MAX_BOOT_GENERATION;
  Engine noWrap(id(101), exhausted, save);
  noWrap.seat(false, 0);
  noWrap.seat(false, 50);
  assert(noWrap.storageFailed && noWrap.durable.outbox.empty());
  assert(noWrap.durable.boot_generation == MAX_BOOT_GENERATION);
}
