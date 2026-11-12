#include <cassert>
#include <cstring>

#include "chair_engine.h"

using namespace SmartChair;

namespace {
  bool saveOk = true;

  bool save(const Durable&) { return saveOk; }

  Header id() {
    Header h;
    std::strcpy(h.node.data(), "chair-01");
    h.boot = 1;
    h.seq  = 1;
    return h;
  }

  Frame nextOf(Engine& e, uint64_t now, Type type) {
    Frame f;
    for (unsigned i = 0; i < 100 && e.next(f, now); ++i)
      if (f.type == type) return f;
    assert(false);
    return {};
  }

  void ack(Engine& e, const Frame& event, uint64_t now) {
    Frame f;
    f.id       = id();
    f.type     = Type::EVENT_ACK;
    f.ack_boot = event.id.boot;
    f.ack_seq  = event.id.seq;
    e.receive(f, now);
  }

  constexpr Sample active{0, 18000, 100, 2000, 2500, 0, 0};
  constexpr Sample quiet{0, 0, 0, 16384, 0, 0, 0};
}  // namespace

int main() {
  Engine e(id(), {}, save);
  e.imuAvailable = true;
  e.seat(true, 0);
  e.seat(true, 50);
  assert(e.radioAction(50, false) == RadioAction::START);
  e.radioReady(250);
  auto sync  = nextOf(e, 250, Type::STATE_SYNC);
  auto ready = nextOf(e, 250, Type::READY);
  assert(ready.reason == Reason::seat_occupied && ready.listen == 5000);
  ack(e, sync, 250);
  e.tick(5249);
  assert(e.communication.state == Communication::OPEN);
  e.tick(5250);
  assert(e.communication.state == Communication::SETTLING);
  assert(e.radioAction(5250, false) == RadioAction::STOP);
  e.radioStopped(5251);
  e.sample(quiet, 6000);
  assert(e.communication.state == Communication::CLOSED);
  e.tick(30049);
  assert(e.communication.state == Communication::CLOSED);
  e.tick(30050);
  assert(e.radioAction(30050, false) == RadioAction::START);
  e.radioReady(30250);
  ready = nextOf(e, 30250, Type::READY);
  assert(ready.reason == Reason::scheduled && ready.listen == 5000);

  Engine motion(id(), {}, save);
  motion.durable.config.radio_interval_s = 100;
  motion.imuAvailable                    = true;
  motion.seat(true, 0);
  motion.seat(true, 50);
  assert(motion.radioAction(50, false) == RadioAction::START);
  motion.radioReady(50);
  sync = nextOf(motion, 50, Type::STATE_SYNC);
  nextOf(motion, 50, Type::READY);
  ack(motion, sync, 50);
  motion.tick(5050);
  assert(motion.radioAction(5050, false) == RadioAction::STOP);
  motion.radioStopped(5050);
  for (uint64_t t : {10000, 10040, 10080, 10120}) motion.sample(active, t);
  assert(motion.radioAction(10120, false) == RadioAction::START);
  motion.radioReady(10120);
  ready = nextOf(motion, 10120, Type::READY);
  assert(ready.reason == Reason::movement_started);
  motion.sampleFailed(10200);
  motion.tick(15118);
  Frame none;
  assert(!motion.next(none, 15118));
  motion.tick(15119);
  ready = nextOf(motion, 15119, Type::READY);
  assert(ready.reason == Reason::scheduled && ready.listen == 10000);
  motion.tick(25000);
  assert(motion.communication.state == Communication::OPEN);
  motion.imuAvailable = true;
  for (uint64_t t : {25040, 25080, 25120, 25160}) motion.sample(quiet, t);
  motion.tick(35159);
  assert(motion.communication.state == Communication::OPEN);
  motion.tick(35160);
  assert(motion.communication.state == Communication::SETTLING);

  // Quiet time across an IMU outage must not count toward confirmed stop.
  Engine outage(id(), {}, save);
  outage.imuAvailable = true;
  outage.seat(true, 0);
  outage.seat(true, 50);
  outage.radioReady(50);
  for (uint64_t t : {100, 140, 180, 220}) outage.sample(active, t);
  outage.sample(quiet, 260);
  assert(outage.operating == Operating::MOTION_STOP_CHECK);
  outage.sampleFailed(300);
  assert(outage.operating == Operating::MOVING);
  outage.imuAvailable = true;
  outage.sample(quiet, 1000);
  assert(outage.operating == Operating::MOTION_STOP_CHECK);
  for (uint64_t t : {1040, 1080, 1120}) outage.sample(quiet, t);
  assert(outage.operating == Operating::OCCUPIED_IDLE);

  Engine failure(id(), {}, save);
  failure.seat(true, 0);
  failure.seat(true, 50);
  assert(failure.radioAction(50, false) == RadioAction::START);
  failure.radioStartFailed(100, true);
  assert(failure.radioAction(1099, false) == RadioAction::NONE);
  assert(failure.radioAction(1100, false) == RadioAction::START);
  failure.radioReady(1100);
  failure.seat(false, 1200);
  failure.seat(false, 1250);
  failure.tick(3750);
  assert(failure.radioAction(3750, false) == RadioAction::STOP);
  failure.radioStopFailed(3750, true);
  assert(failure.communication.state == Communication::SETTLING);
  Frame noSend;
  assert(!failure.next(noSend, 3751));
  assert(failure.radioAction(4749, false) == RadioAction::NONE);
  assert(failure.radioAction(4750, false) == RadioAction::STOP);
  failure.radioStopped(4750);
  failure.tick(4750);
  assert(failure.operating == Operating::DEEP_SLEEP);

  // Startup time is charged against the one vacant preparation budget.
  Engine vacant(id(), {}, save);
  vacant.seat(false, 0);
  vacant.seat(false, 50);
  assert(vacant.radioAction(50, false) == RadioAction::START);
  vacant.radioReady(450);
  assert(vacant.communication.state == Communication::SETTLING);
  assert(vacant.radioAction(2549, false) == RadioAction::NONE);
  assert(vacant.radioAction(2550, false) == RadioAction::STOP);
  vacant.radioStopped(2550);
  assert(vacant.operating == Operating::DEEP_SLEEP);

  // Previously persisted backlog cannot reopen a closed radio before the next schedule.
  Engine backlog(id(), {}, save);
  backlog.seat(true, 0);
  backlog.seat(true, 50);
  assert(backlog.radioAction(50, false) == RadioAction::START);
  backlog.radioReady(50);
  nextOf(backlog, 50, Type::STATE_SYNC);
  nextOf(backlog, 50, Type::READY);
  backlog.tick(5050);
  assert(backlog.radioAction(7549, false) == RadioAction::NONE);
  assert(backlog.radioAction(7550, false) == RadioAction::STOP);
  backlog.radioStopped(7550);
  backlog.tick(20000);
  assert(backlog.radioAction(20000, false) == RadioAction::NONE);
  backlog.tick(120050);  // Skip missed periods and open once.
  assert(backlog.radioAction(120050, false) == RadioAction::START);
  backlog.radioReady(120050);
  backlog.tick(125050);
  assert(backlog.radioAction(127550, false) == RadioAction::STOP);
  backlog.radioStopped(127550);
  backlog.tick(150049);
  assert(backlog.radioAction(150049, false) == RadioAction::NONE);
  backlog.tick(150050);
  assert(backlog.radioAction(150050, false) == RadioAction::START);

  // A failed durable transaction cannot adopt the requested policy.
  Engine policy(id(), {}, save);
  policy.seat(true, 0);
  policy.seat(true, 50);
  policy.radioReady(50);
  Frame command;
  command.id                           = id();
  command.type                         = Type::CONFIG_SET;
  command.command[0]                   = 1;
  command.config.revision              = 1;
  command.config.radio_windows_enabled = false;
  saveOk                               = false;
  policy.receive(command, 100);
  assert(policy.durable.config.radio_windows_enabled);
  saveOk = true;
  policy.receive(command, 101);
  assert(!policy.durable.config.radio_windows_enabled);
  assert(policy.durable.config.revision == 1);

  Engine shorter(id(), {}, save);
  shorter.seat(true, 0);
  shorter.seat(true, 50);
  shorter.radioReady(50);
  command.command[0]                   = 3;
  command.config.radio_windows_enabled = true;
  command.config.radio_open_s          = 2;
  shorter.receive(command, 100);
  assert(shorter.communication.closes == 2100);

  // Distinct physical transitions survive a temporary storage failure in order.
  Engine retry(id(), {}, save);
  retry.seat(true, 0);
  retry.seat(true, 50);
  saveOk = false;
  retry.seat(false, 100);
  retry.seat(false, 150);
  retry.seat(true, 200);
  retry.seat(true, 250);
  assert(retry.storageFailed);
  saveOk = true;
  retry.tick(300);
  assert(!retry.storageFailed && retry.durable.outbox.size() == 3);
  assert(retry.durable.outbox[1].type == Type::VACATED);
  assert(retry.durable.outbox[2].type == Type::OCCUPIED);
  assert(retry.durable.outbox[1].id.seq < retry.durable.outbox[2].id.seq);

  // Prolonged storage failure near sequence exhaustion never reuses a persisted identity.
  Header nearWrap = id();
  nearWrap.seq    = UINT32_MAX - 2;
  Engine exhausted(nearWrap, {}, save);
  exhausted.durable.config.heartbeat_s = 1;
  saveOk                               = false;
  exhausted.seat(true, 0);
  exhausted.seat(true, 50);
  exhausted.seat(false, 100);
  exhausted.seat(false, 150);
  exhausted.seat(true, 200);
  exhausted.seat(true, 250);
  assert(exhausted.identity.seq == UINT32_MAX && exhausted.storageFailed);
  saveOk = true;
  exhausted.tick(1250);  // Heartbeat and recovery can occur in the same iteration.
  assert(!exhausted.storageFailed && exhausted.durable.outbox.size() == 1);
  assert(exhausted.durable.outbox[0].type == Type::STATE_SYNC);
  assert(exhausted.durable.outbox[0].id.seq == UINT32_MAX - 1);
  assert(exhausted.durable.outbox[0].common.occupied);
  exhausted.rotateBoot(2, 1251);
  assert(exhausted.durable.outbox.size() == 2);
  assert(exhausted.durable.outbox[1].id.boot == 2);
  assert(exhausted.durable.outbox[1].id.seq == 1);

  Engine boundary(id(), {}, save);
  boundary.seat(true, 0);
  boundary.seat(true, 50);
  boundary.radioReady(50);
  sync = nextOf(boundary, 50, Type::STATE_SYNC);
  nextOf(boundary, 50, Type::READY);
  ack(boundary, sync, 50);
  command.command[0]                   = 2;
  command.config.radio_windows_enabled = true;
  command.config.radio_interval_s      = 60;
  command.config.radio_open_s          = 5;
  boundary.receive(command, 5050);
  assert(boundary.communication.state == Communication::SETTLING);
  assert(boundary.communication.deadline == 7550);
  assert(nextOf(boundary, 5050, Type::COMMAND_RESULT).status == Status::applied);
  while (boundary.next(noSend, 5050)) assert(noSend.type != Type::READY);

  // Reseating after shutdown has begun waits for stop completion before reopening.
  Engine reseat(id(), {}, save);
  reseat.seat(true, 0);
  reseat.seat(true, 50);
  assert(reseat.radioAction(50, false) == RadioAction::START);
  reseat.radioReady(50);
  reseat.seat(false, 100);
  reseat.seat(false, 150);
  assert(reseat.radioAction(2650, false) == RadioAction::STOP);
  reseat.seat(true, 2660);
  reseat.seat(true, 2710);
  assert(reseat.communication.state == Communication::SETTLING);
  reseat.radioStopFailed(2711, true);
  assert(reseat.radioAction(3710, false) == RadioAction::NONE);
  assert(reseat.radioAction(3711, false) == RadioAction::STOP);
  reseat.radioStopped(3711);
  assert(reseat.communication.state == Communication::OPENING);
}
