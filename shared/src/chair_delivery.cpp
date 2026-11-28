#include <algorithm>
#include <utility>

#include "chair_engine.h"

namespace SmartChair {
  Frame Engine::frame(Type type, uint64_t now) {
    Frame f;
    f.type            = type;
    f.id              = identity;
    f.boot_generation = durable.boot_generation;
    // UINT32_MAX is a sentinel: never allocate the same identity twice if storage is down.
    if (identity.seq == UINT32_MAX)
      f.id.seq = 0;
    else
      ++identity.seq;
    f.common = {uint32_t(now), occupied, batteryMv_, durable.config.revision,
                uint8_t((batteryMv_ == UINT16_MAX ? Health::BATTERY_UNAVAILABLE : 0) |
                        (imuAvailable ? 0 : Health::IMU_UNAVAILABLE))};
    return f;
  }

  void Engine::rotateBoot(uint64_t boot, uint64_t now) {
    if (!boot || boot == identity.boot || statePending_) return;

    identity.boot = boot;
    identity.seq  = 1;
    newBoot_      = true;
    queueEvent(Type::STATE_SYNC, now);
  }

  void Engine::ready(Reason reason, uint64_t now) {
    for (const auto& pending : best_)
      if (pending.type == Type::READY && pending.reason < reason) reason = pending.reason;
    // A routine refresh replaces announcements, but a pending probe still needs its proof.
    std::erase_if(best_, [](const Frame& f) { return f.type == Type::READY; });
    readyRefresh_ = 0;
    auto f        = frame(Type::READY, now);
    if (!f.id.seq) return;
    if (best_.size() >= Limits::BEST_EFFORT_QUEUE) discardBest();
    f.reason = reason;
    best_.push_back(std::move(f));
  }

  void Engine::clearReady() {
    std::erase_if(
        best_, [](const Frame& f) { return f.type == Type::READY || f.type == Type::READY_PROOF; });
    readyRefresh_ = 0;
  }

  bool Engine::next(Frame& output, uint64_t now) {
    if (!radioReady_ || shutdownStarted_ ||
        (communication.state != Communication::OPEN &&
         communication.state != Communication::SETTLING))
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
    if (droppedBatches_) {
      output = frame(Type::MOVEMENT_LOSS, now);
      if (!output.id.seq) return false;
      output.dropped_batches = droppedBatches_;
      droppedBatches_        = 0;
      return true;
    }
    if (best_.empty()) return false;
    output = std::move(best_.front());
    best_.pop_front();
    if (output.type == Type::READY || output.type == Type::READY_PROOF) {
      output.listen = communication.remaining(now);
      if (!occupied || communication.state != Communication::OPEN || !output.listen) return false;
      if (output.type == Type::READY && motionHold_ && output.listen != UINT32_MAX)
        readyRefresh_ = deadlineAfter(now, std::max<uint32_t>(1, (output.listen - 1) / 2));
    }
    return true;
  }

  void Engine::discardBest() {
    // Preserve the single pending proof while making room for routine telemetry.
    auto discarded = std::find_if(best_.begin(), best_.end(),
                                  [](const Frame& f) { return f.type != Type::READY_PROOF; });
    if (discarded == best_.end()) discarded = best_.begin();
    if (discarded->type == Type::MOVEMENT_BATCH && droppedBatches_ != UINT32_MAX) ++droppedBatches_;
    best_.erase(discarded);
  }

  void Engine::receive(const Frame& f, uint64_t now) {
    if (!valid(f) || f.id.node != identity.node) return;
    if (motionHold_ && occupied && communication.state == Communication::OPEN &&
        durable.config.radio_windows_enabled)
      communication.closes =
          std::max(communication.closes,
                   deadlineAfter(now, milliseconds(durable.config.radio_motion_tail_s)));
    if (communication.state == Communication::OPEN && !communication.unlimited &&
        now >= communication.closes) {
      communication.settle(communication.closes);
      clearReady();
    }
    if (f.type == Type::EVENT_ACK) {
      acknowledge(f);
      return;
    }
    if (f.type == Type::READY_PROBE) {
      if (!occupied || !radioReady_ || communication.state != Communication::OPEN ||
          shutdownStarted_)
        return;
      std::erase_if(best_, [](const Frame& queued) { return queued.type == Type::READY_PROOF; });
      auto proof = frame(Type::READY_PROOF, now);
      if (!proof.id.seq) return;
      proof.ack_boot = f.id.boot;
      proof.ack_seq  = f.id.seq;
      if (best_.size() >= Limits::BEST_EFFORT_QUEUE) discardBest();
      best_.push_front(std::move(proof));
      return;
    }
    if (f.type != Type::CONFIG_SET) return;
    // One additional identity may be needed for a coalesced STATE_SYNC.
    if (identity.seq >= UINT32_MAX - 1) return;
    if (statePending_) {
      auto receipt    = frame(Type::COMMAND_RECEIPT, now);
      receipt.command = f.command;
      receipt.status  = Status::busy;
      if (receipt.id.seq && best_.size() < Limits::BEST_EFFORT_QUEUE) best_.push_back(receipt);
      return;
    }
    const auto* record  = findCommand(f.command);
    const Status result = commandResult(f, record);
    if (record && resultQueued(f.command, result)) return;

    if (!hasCommandCapacity()) {
      auto receipt    = frame(Type::COMMAND_RECEIPT, now);
      receipt.command = f.command;
      receipt.status  = Status::busy;
      if (receipt.id.seq && best_.size() < Limits::BEST_EFFORT_QUEUE) best_.push_back(receipt);
      return;
    }
    Durable changed           = durable;
    const auto previousConfig = durable.config;
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
      const auto& nextConfig = durable.config;
      const bool policyChanged =
          previousConfig.radio_windows_enabled != nextConfig.radio_windows_enabled ||
          previousConfig.radio_interval_s != nextConfig.radio_interval_s ||
          previousConfig.radio_open_s != nextConfig.radio_open_s ||
          previousConfig.radio_motion_tail_s != nextConfig.radio_motion_tail_s;
      if (occupied && policyChanged) {
        clearReady();
        scheduleActive_ = true;
        nextSchedule_   = deadlineAfter(now, milliseconds(nextConfig.radio_interval_s));
        if (communication.state == Communication::SETTLING) {
          // A command received at expiry can finish in settling without renewing the lease.
          if (!nextConfig.radio_windows_enabled) reopenAfterStop_ = true;
        } else {
          const auto openTime     = milliseconds(nextConfig.radio_open_s);
          communication.unlimited = !nextConfig.radio_windows_enabled;
          if (communication.state == Communication::OPEN)
            communication.closes = deadlineAfter(now, openTime);
          else if (communication.state == Communication::OPENING)
            communication.duration = openTime;
          requestOpen(Reason::scheduled, now);
        }
      }
      if (occupied && !policyChanged && communication.state == Communication::CLOSED)
        requestOpen(Reason::scheduled, now);

      if (!durable.config.movement_enabled) {
        if (moving_) {
          flush(End::disabled, now);
          if (occupied && communication.state == Communication::OPEN &&
              durable.config.radio_windows_enabled)
            communication.extend(now, milliseconds(durable.config.radio_motion_tail_s));
        }
        batch_.clear();
        candidateMotion_ = false;
        motionHold_      = false;
        moving_ = stopping_ = false;
        if (occupied) operating = Operating::OCCUPIED_IDLE;
      }
      auto receipt    = frame(Type::COMMAND_RECEIPT, now);
      receipt.command = f.command;
      receipt.status  = Status::received;
      if (receipt.id.seq && best_.size() < Limits::BEST_EFFORT_QUEUE) best_.push_back(receipt);
    }
  }

  void Engine::acknowledge(const Frame& ack) {
    Durable changed     = durable;
    bool batteryAlerted = false;
    std::erase_if(changed.outbox, [&](const auto& event) {
      if (!matchesAck(event.id, ack)) return false;
      batteryAlerted |= event.type == Type::BATTERY_LOW;
      return true;
    });
    if (changed.outbox.size() == durable.outbox.size() || !commit(changed)) return;
    if (batteryAlerted) batteryLow = true;

    if (durable.outbox.empty()) delivery = Delivery::IDLE;

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

  bool Engine::batteryAlertQueued() const {
    const auto alert = [](const Frame& f) { return f.type == Type::BATTERY_LOW; };
    return std::ranges::any_of(durable.outbox, alert) ||
           std::ranges::any_of(pendingStorage_, alert);
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
    Frame event;
    if (identity.seq == UINT32_MAX) {
      if (!statePending_ || pendingStorage_.empty()) {
        statePending_ = storageFailed = true;
        return;
      }
      // Reuse only an identity that was never persisted or transmitted.
      const auto pendingId = pendingStorage_.back().id;
      pendingStorage_.clear();
      event    = frame(Type::STATE_SYNC, now);
      event.id = pendingId;
    } else {
      event = frame(type, now);
    }
    if (pendingStorage_.size() >= Limits::OUTBOX_CAPACITY) {
      // As with the durable outbox, bounded overflow preserves current state, not history.
      pendingStorage_.clear();
      event.type = Type::STATE_SYNC;
    }
    pendingStorage_.push_back(std::move(event));
    statePending_ = true;
    persistPending(now);
  }

  void Engine::persistPending(uint64_t now) {
    if (pendingStorage_.empty()) {
      statePending_ = storageFailed = true;
      return;
    }
    Durable changed = durable;
    if (newBoot_) {
      if (changed.boot_generation == MAX_BOOT_GENERATION) {
        statePending_ = storageFailed = true;
        return;
      }
      ++changed.boot_generation;
    }

    for (auto event : pendingStorage_) {
      if (identity.seq == UINT32_MAX && changed.outbox.size() >= Limits::MAX_COMMAND_RESULTS) {
        std::erase_if(changed.outbox, [](const auto& f) { return f.type != Type::COMMAND_RESULT; });
        event                 = pendingStorage_.back();
        event.type            = Type::STATE_SYNC;
        event.boot_generation = changed.boot_generation;
        changed.outbox.push_back(std::move(event));
        break;
      }
      reserve(changed, now);
      if (changed.outbox.size() >= Limits::OUTBOX_CAPACITY) {
        // Protected results must survive; replace only the reserved state snapshot.
        std::erase_if(changed.outbox, [](const auto& f) { return f.type == Type::STATE_SYNC; });
        event.type = Type::STATE_SYNC;
      }
      event.boot_generation = changed.boot_generation;
      changed.outbox.push_back(std::move(event));
    }
    statePending_ = !commit(changed);
    if (!statePending_) {
      newBoot_ = false;
      pendingStorage_.clear();
    }
    storageFailed = statePending_;
    if (!statePending_ && occupied) requestOpen(Reason::scheduled, now);
  }
}  // namespace SmartChair
