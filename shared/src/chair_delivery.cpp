#include <algorithm>
#include <utility>

#include "chair_engine.h"

namespace SmartChair {
  Frame Engine::frame(Type type, uint64_t now) {
    Frame f;
    f.type            = type;
    f.id              = identity;
    f.boot_generation = durable.boot_generation;
    // The hardware adapter rotates boot identity before sequence exhaustion.
    if (identity.seq != UINT32_MAX) ++identity.seq;
    f.common = {
        uint32_t(now), occupied, UINT16_MAX, durable.config.revision,
        uint8_t(Health::BATTERY_UNAVAILABLE | (imuAvailable ? 0 : Health::IMU_UNAVAILABLE))};
    return f;
  }

  void Engine::rotateBoot(uint64_t boot, uint64_t now) {
    if (!boot || boot == identity.boot) return;

    identity.boot = boot;
    identity.seq  = 1;
    newBoot_      = true;
    queueEvent(Type::STATE_SYNC, now);
  }

  void Engine::ready(Reason reason, uint64_t now) {
    if (best_.size() >= Limits::BEST_EFFORT_QUEUE) best_.pop_front();
    auto f   = frame(Type::READY, now);
    f.reason = reason;
    best_.push_back(std::move(f));
  }

  bool Engine::next(Frame& output, uint64_t now) {
    if (communication.state != Communication::OPEN &&
        communication.state != Communication::SETTLING)
      return false;
    std::erase_if(rounds_, [&](const auto& r) {
      return std::none_of(durable.outbox.begin(), durable.outbox.end(),
                          [&](const auto& f) { return r.id == f.id; });
    });
    for (const auto& f : durable.outbox) {
      auto it =
          std::find_if(rounds_.begin(), rounds_.end(), [&](const auto& r) { return r.id == f.id; });
      if (it == rounds_.end()) {
        rounds_.push_back({f.id, now, 0});
        it = rounds_.end() - 1;
      }
      if (now - it->start >= Timing::DELIVERY_ROUND_MS) {
        it->start   = now;
        it->attempt = 0;
      }
      if (it->attempt < Timing::RETRY_OFFSETS_MS.size() &&
          now - it->start >= Timing::RETRY_OFFSETS_MS[it->attempt]) {
        ++it->attempt;
        output   = f;
        delivery = Delivery::RETRYING;
        return true;
      }
    }
    delivery = durable.outbox.empty() ? Delivery::IDLE : Delivery::WAITING;
    if (best_.empty()) return false;
    output = std::move(best_.front());
    best_.pop_front();
    if (output.type == Type::READY) output.listen = communication.remaining(now);
    return true;
  }

  void Engine::receive(const Frame& f, uint64_t now) {
    if (!valid(f) || f.id.node != identity.node) return;
    if (f.type == Type::EVENT_ACK) {
      acknowledge(f);
      return;
    }
    if (f.type != Type::CONFIG_SET) return;
    const auto* record  = findCommand(f.command);
    const Status result = commandResult(f, record);
    if (record && resultQueued(f.command, result)) return;

    if (!hasCommandCapacity()) {
      auto receipt    = frame(Type::COMMAND_RECEIPT, now);
      receipt.command = f.command;
      receipt.status  = Status::busy;
      if (best_.size() < Limits::BEST_EFFORT_QUEUE) best_.push_back(receipt);
      return;
    }
    Durable changed = durable;
    // A repeated UUID returns its historical result without rolling back newer settings.
    if (record == nullptr && result == Status::applied) changed.config = f.config;
    if (record == nullptr && !rememberCommand(changed, f, result)) return;
    auto event            = frame(Type::COMMAND_RESULT, now);
    event.command         = f.command;
    event.status          = result;
    event.common.revision = changed.config.revision;
    reserve(changed, now);
    changed.outbox.push_back(event);
    if (commit(changed)) {
      if (communication.state == Communication::CLOSED) communication.open();

      if (!durable.config.movement_enabled) {
        if (moving_) flush(End::disabled, now);
        batch_.clear();
        candidateMotion_ = false;
        moving_ = stopping_ = false;
        if (occupied) operating = Operating::OCCUPIED_IDLE;
      }
      auto receipt    = frame(Type::COMMAND_RECEIPT, now);
      receipt.command = f.command;
      receipt.status  = Status::received;
      if (best_.size() < Limits::BEST_EFFORT_QUEUE) best_.push_back(receipt);
    }
  }

  void Engine::acknowledge(const Frame& ack) {
    Durable changed = durable;
    std::erase_if(changed.outbox, [&](const auto& event) { return matchesAck(event.id, ack); });
    if (changed.outbox.size() == durable.outbox.size() || !commit(changed)) return;

    std::erase_if(rounds_, [&](const auto& round) { return matchesAck(round.id, ack); });
  }

  const CommandRecord* Engine::findCommand(const Uuid& uuid) const {
    const auto record = std::find_if(durable.commands.begin(), durable.commands.end(),
                                     [&](const auto& command) { return command.uuid == uuid; });
    return record == durable.commands.end() ? nullptr : &*record;
  }

  Status Engine::commandResult(const Frame& command, const CommandRecord* previous) const {
    if (previous)
      return previous->config == command.config ? previous->status : Status::command_conflict;
    if (command.config.revision < durable.config.revision) return Status::stale_revision;
    if (command.config.revision == durable.config.revision)
      return command.config == durable.config ? Status::already_applied : Status::revision_conflict;
    return Status::applied;
  }

  bool Engine::resultQueued(const Uuid& uuid, Status status) const {
    return std::any_of(durable.outbox.begin(), durable.outbox.end(), [&](const auto& event) {
      return event.type == Type::COMMAND_RESULT && event.command == uuid && event.status == status;
    });
  }

  bool Engine::hasCommandCapacity() const {
    const auto protectedResults =
        std::count_if(durable.outbox.begin(), durable.outbox.end(),
                      [](const auto& event) { return event.type == Type::COMMAND_RESULT; });
    // Keep one of the 32 slots available for current-state recovery.
    return std::cmp_less(protectedResults, Limits::MAX_COMMAND_RESULTS);
  }

  bool Engine::canForgetCommand(const Uuid& uuid) const {
    return std::none_of(durable.outbox.begin(), durable.outbox.end(), [&](const auto& event) {
      return event.type == Type::COMMAND_RESULT && event.command == uuid;
    });
  }

  bool Engine::rememberCommand(Durable& changed, const Frame& command, Status result) const {
    if (changed.commands.size() == Limits::COMMAND_RECORDS) {
      // Preserve original request content until its reliable result is durably acknowledged.
      const auto oldestAcknowledged =
          std::find_if(changed.commands.begin(), changed.commands.end(),
                       [&](const auto& record) { return canForgetCommand(record.uuid); });
      if (oldestAcknowledged == changed.commands.end()) return false;

      changed.commands.erase(oldestAcknowledged);
    }

    changed.commands.push_back({command.command, command.config, result});
    return true;
  }

  bool Engine::commit(const Durable& d) {
    if (!save_(d)) {
      storageFailed = true;
      return false;
    }
    durable       = d;
    storageFailed = statePending_;
    return true;
  }

  void Engine::reserve(Durable& d, uint64_t now) {
    if (d.outbox.size() < Limits::MAX_COMMAND_RESULTS) return;
    std::erase_if(d.outbox, [](const auto& f) { return f.type != Type::COMMAND_RESULT; });
    auto sync            = frame(Type::STATE_SYNC, now);
    sync.common.revision = d.config.revision;
    sync.boot_generation = d.boot_generation;
    d.outbox.push_back(std::move(sync));
  }

  void Engine::queueEvent(Type type, uint64_t now) {
    Durable changed = durable;
    if (newBoot_) {
      if (changed.boot_generation == MAX_BOOT_GENERATION) {
        statePending_ = storageFailed = true;
        return;
      }
      ++changed.boot_generation;
    }

    reserve(changed, now);
    if (changed.outbox.size() >= Limits::OUTBOX_CAPACITY) {
      // Protected results must survive; replace only the reserved state snapshot.
      std::erase_if(changed.outbox, [](const auto& f) { return f.type == Type::STATE_SYNC; });
      type = Type::STATE_SYNC;
    }
    auto event            = frame(type, now);
    event.boot_generation = changed.boot_generation;
    changed.outbox.push_back(std::move(event));
    statePending_ = !commit(changed);
    if (!statePending_) newBoot_ = false;
    storageFailed = statePending_;
    if (!statePending_ && communication.state == Communication::CLOSED) communication.open();
  }
}  // namespace SmartChair
