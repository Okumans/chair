#pragma once

#include <algorithm>
#include <cmath>
#include <deque>

#include "protocol.h"

namespace SmartChair {
  /// Physical sensing and sleep states. Seat release is handled from every occupied state.
  enum class Operating {
    STARTING,
    OCCUPIED_IDLE,
    SAMPLE_CHECK,
    MOTION_START_CHECK,
    MOVING,
    MOTION_STOP_CHECK,
    OCCUPIED_LIGHT_SLEEP,
    PREPARE_SLEEP,
    DEEP_SLEEP
  };
  /// Independent radio lifecycle; only CLOSED permits future occupied sleep.
  enum class Communication { CLOSED, OPENING, OPEN, SETTLING };
  /// Observable outbox status; MAC delivery never substitutes for a durable Pi ACK.
  enum class Delivery { IDLE, RETRYING, WAITING };

  /// Radio-window timing model. All times are monotonic uint64 milliseconds.
  struct Window {
    Communication state = Communication::CLOSED;
    uint64_t duration = 0, closes = 0, deadline = 0;
    bool unlimited = false;

    /// Request radio opening. A zero duration means indefinitely open while awake.
    void open(uint64_t length = 0) {
      state     = Communication::OPENING;
      duration  = length;
      unlimited = length == 0;
    }

    /// Start the requested window when the hardware reports the radio ready.
    void radioReady(uint64_t now) {
      if (state == Communication::OPENING) {
        state  = Communication::OPEN;
        closes = now + duration;
      }
    }

    /// Open or extend availability by length milliseconds; opening time is not charged to it.
    void extend(uint64_t now, uint64_t length) {
      if (state == Communication::CLOSED) {
        open(length);
      } else if (state == Communication::OPENING) {
        duration  = std::max(duration, length);
        unlimited = unlimited || length == 0;
      } else {
        unlimited = unlimited || length == 0;
        state     = Communication::OPEN;
        closes    = std::max(closes, now + length);
      }
    }

    /// Begin bounded shutdown, allowing at most 2500 ms for pending work.
    void settle(uint64_t now) {
      state    = Communication::SETTLING;
      deadline = now + 2500;
    }

    /// Advance expiry and shutdown; pending work may delay closing only until the deadline.
    void tick(uint64_t now, bool pending) {
      if (state == Communication::OPEN && !unlimited && now >= closes) settle(now);
      if (state == Communication::SETTLING && (!pending || now >= deadline))
        state = Communication::CLOSED;
    }

    /// Return true only when the radio lifecycle is fully closed.
    bool allowsSleep() const { return state == Communication::CLOSED; }

    /// Compute listen time at transmission; UINT32_MAX means no scheduled close.
    uint32_t remaining(uint64_t now) const {
      return unlimited
                 ? UINT32_MAX
                 : uint32_t(std::min<uint64_t>(UINT32_MAX - 1, closes > now ? closes - now : 0));
    }
  };

  /// Bounded durable UUID deduplication record, including original content and outcome.
  struct CommandRecord {
    Uuid uuid{};
    Configuration config;
    Status status = Status::applied;
  };

  /// Atomic storage snapshot: configuration, up to 32 reliable events and 32 command records.
  struct Durable {
    Configuration config;
    std::vector<Frame> outbox;
    std::vector<CommandRecord> commands;
  };

  /// Serialize a trusted engine snapshot into the versioned, explicit byte storage format.
  inline std::vector<uint8_t> encodeDurable(const Durable& d) {
    Writer w;
    w.put(0x31484353, 4);
    w.put(d.config.revision, 4);
    w.put(d.config.heartbeat_s, 4);
    w.put(d.config.movement_batch_s, 4);
    w.put(d.config.movement_enabled, 1);
    w.put(d.outbox.size(), 1);
    for (const auto& f : d.outbox) {
      const auto bytes = encode(f);
      w.put(bytes.size(), 2);
      w.bytes.insert(w.bytes.end(), bytes.begin(), bytes.end());
    }
    w.put(d.commands.size(), 1);
    for (const auto& c : d.commands) {
      for (auto b : c.uuid) w.put(b, 1);
      w.put(c.config.revision, 4);
      w.put(c.config.heartbeat_s, 4);
      w.put(c.config.movement_batch_s, 4);
      w.put(c.config.movement_enabled, 1);
      w.put(uint8_t(c.status), 1);
    }
    return w.bytes;
  }

  /// Validate a stored snapshot completely; leave out unchanged on corrupt or unsupported input.
  inline bool decodeDurable(std::span<const uint8_t> bytes, Durable& out) {
    Reader r{bytes};
    if (r.get(4) != 0x31484353) return false;
    Durable d;
    d.config.revision         = r.get(4);
    d.config.heartbeat_s      = r.get(4);
    d.config.movement_batch_s = r.get(4);
    auto enabled              = r.get(1);
    if (enabled > 1) return false;
    d.config.movement_enabled = enabled;
    auto count                = r.get(1);
    if (count > 32 || !valid(d.config)) return false;
    for (size_t i = 0; i < count; ++i) {
      auto length = r.get(2);
      if (!r.ok || length > bytes.size() - r.pos) return false;
      Frame f;
      if (!decode(bytes.subspan(r.pos, length), f) || !reliable(f.type)) return false;
      r.pos += length;
      d.outbox.push_back(std::move(f));
    }
    count = r.get(1);
    if (count > 32) return false;
    for (size_t i = 0; i < count; ++i) {
      CommandRecord c;
      for (auto& b : c.uuid) b = r.get(1);
      c.config.revision         = r.get(4);
      c.config.heartbeat_s      = r.get(4);
      c.config.movement_batch_s = r.get(4);
      enabled                   = r.get(1);
      if (enabled > 1) return false;
      c.config.movement_enabled = enabled;
      c.status                  = Status(r.get(1));
      if (!nonzero(c.uuid) || !valid(c.config) || c.status > Status::command_conflict) return false;
      d.commands.push_back(c);
    }
    if (!r.ok || r.pos != bytes.size()) return false;
    out = std::move(d);
    return true;
  }

  /// Cooperative chair logic with hardware-independent time and samples.
  /// The save adapter must durably commit the entire snapshot before returning true.
  /// No radio, sleep or GPIO work runs here; callers execute those actions between updates.
  class Engine {
   public:
    /// Atomic persistence adapter. False keeps the previous durable snapshot authoritative.
    using Save = bool (*)(const Durable&);
    Durable durable;
    Operating operating = Operating::STARTING;
    Window communication;
    Delivery delivery = Delivery::IDLE;
    bool occupied = false, imuAvailable = false, storageFailed = false;
    uint32_t occupied_sleep_ms  = 0;
    // Internal future-window policy only; no hardware schedule or CONFIG_SET field is enabled.
    uint64_t movement_window_ms = 0;
    Header identity;

    /// Restore durable work under the current wake identity and request startup communication.
    Engine(Header id, Durable data, Save save)
        : durable(std::move(data)), identity(id), save_(save) {
      communication.open();
    }

    /// Allocate an event identity and snapshot. The adapter rotates boot before sequence
    /// exhaustion.
    Frame frame(Type type, uint64_t now) {
      Frame f;
      f.type = type;
      f.id   = identity;
      // The hardware adapter rotates boot identity before sequence exhaustion.
      if (identity.seq != UINT32_MAX) ++identity.seq;
      f.common = {uint32_t(now), occupied, UINT16_MAX, durable.config.revision,
                  uint8_t(2 | (imuAvailable ? 0 : 1))};
      return f;
    }

    /// Complete radio opening and announce occupied availability with a fresh READY.
    void radioReady(uint64_t now) {
      const bool opening = communication.state == Communication::OPENING;
      communication.radioReady(now);
      if (opening && occupied) {
        ready(openingReason_, now);
        openingReason_ = Reason::scheduled;
      }
    }

    /// Queue fresh RAM-only availability. Remaining listen time is filled when next() transmits it.
    void ready(Reason reason, uint64_t now) {
      if (best_.size() >= 4) best_.pop_front();
      auto f   = frame(Type::READY, now);
      f.reason = reason;
      best_.push_back(std::move(f));
    }

    /// Tell the adapter when the next 40 ms occupied sample is due, without catch-up spinning.
    bool sampleDue(uint64_t now) const { return occupied && now >= nextSample_; }

    /// Apply a raw seat sample with 50 ms debounce; timerWake selects the startup heartbeat event.
    void seat(bool raw, uint64_t now, bool timerWake = false) {
      if (!seatSeen_ || raw != candidate_) {
        seatSeen_  = true;
        candidate_ = raw;
        seatAt_    = now;
      }
      if (now - seatAt_ < 50) return;
      if (operating == Operating::STARTING || occupied != candidate_) {
        const bool starting = operating == Operating::STARTING;
        if (!candidate_ && moving_) flush(End::vacant, now);
        occupied = candidate_;
        moving_ = stopping_ = candidateMotion_ = false;
        batch_.clear();
        operating    = occupied ? Operating::OCCUPIED_IDLE : Operating::PREPARE_SLEEP;
        sleepAt_     = now;
        nextSample_  = now;
        heartbeatAt_ = now;
        queueEvent(starting ? (timerWake ? Type::HEARTBEAT : Type::STATE_SYNC)
                            : (occupied ? Type::OCCUPIED : Type::VACATED),
                   now);
        if (occupied) {
          openingReason_ = Reason::seat_occupied;
          communication.open();
        } else
          communication.settle(now);
        rounds_.clear();
      }
    }

    /// Classify one raw acquisition, retain debounce samples and flush bounded movement batches.
    /// now is the actual acquisition time in monotonic milliseconds; raw axis values stay
    /// unchanged.
    void sample(Sample raw, uint64_t now) {
      nextSample_ = now + 40;
      if (!occupied || !imuAvailable || !durable.config.movement_enabled) return;
      const double magnitude =
          std::sqrt(double(raw.ax) * raw.ax + double(raw.ay) * raw.ay + double(raw.az) * raw.az) /
          16384.;
      const double accel = std::abs(magnitude - 1.0);
      const double gyro =
          std::max({std::abs(int(raw.gx)), std::abs(int(raw.gy)), std::abs(int(raw.gz))}) / 131.;
      const bool active = accel > .15 || gyro > 15;
      const bool quiet  = accel < .08 && gyro < 8;
      if (!moving_) {
        operating = Operating::SAMPLE_CHECK;
        if (!active) {
          batch_.clear();
          candidateMotion_ = false;
          operating        = Operating::OCCUPIED_IDLE;
          return;
        }
        if (!candidateMotion_) {
          candidateMotion_ = true;
          motionAt_        = now;
          batchAt_ = intervalAt_ = now;
        }
        operating = Operating::MOTION_START_CHECK;
        append(raw, now);
        if (now - motionAt_ < 100) return;
        moving_          = true;
        candidateMotion_ = false;
        stopping_        = false;
        operating        = Operating::MOVING;
        communication.extend(now, movement_window_ms);
        if (communication.state == Communication::OPEN)
          ready(Reason::movement_started, now);
        else
          openingReason_ = Reason::movement_started;
      } else {
        append(raw, now);
        if (quiet) {
          if (!stopping_) {
            stopping_ = true;
            motionAt_ = now;
          }
          operating = Operating::MOTION_STOP_CHECK;
          if (now - motionAt_ >= 100) {
            flush(End::stopped, now);
            moving_ = stopping_ = false;
            operating           = Operating::OCCUPIED_IDLE;
            return;
          }
        } else {
          stopping_ = false;
          operating = Operating::MOVING;
        }
      }
      if (batch_.size() == MAX_SAMPLES)
        flush(End::capacity, now);
      else if (now - intervalAt_ >= milliseconds(durable.config.movement_batch_s))
        flush(End::interval, now);
    }

    /// Retry failed state persistence, queue heartbeats and advance bounded vacant sleep.
    void tick(uint64_t now, bool radioBusy = false) {
      if (statePending_ && now >= storageRetry_) {
        storageRetry_ = now + 250;
        queueEvent(Type::STATE_SYNC, now);
      }
      if (occupied && now - heartbeatAt_ >= milliseconds(durable.config.heartbeat_s)) {
        heartbeatAt_ = now;
        queueEvent(Type::HEARTBEAT, now);
      }
      communication.tick(now, radioBusy || !durable.outbox.empty() || !best_.empty());
      if (operating == Operating::OCCUPIED_IDLE && canOccupiedSleep() && now < nextSample_)
        operating = Operating::OCCUPIED_LIGHT_SLEEP;
      if (operating == Operating::OCCUPIED_LIGHT_SLEEP && now >= nextSample_)
        operating = Operating::SAMPLE_CHECK;
      if (operating == Operating::PREPARE_SLEEP && !storageFailed && !candidate_ &&
          ((!radioBusy && durable.outbox.empty() && best_.empty()) || now - sleepAt_ >= 2500))
        operating = Operating::DEEP_SLEEP;
    }

    /// Allow future occupied sleep only with a nonzero duration, quiet motion and a closed radio.
    bool canOccupiedSleep() const {
      return occupied_sleep_ms && occupied && !moving_ && communication.allowsSleep();
    }

    /// Take one due radio frame without waiting. Reliable data stays in the outbox until exact ACK.
    /// Call only when the radio can accept a send; best-effort frames are consumed when returned.
    bool next(Frame& output, uint64_t now) {
      if (communication.state != Communication::OPEN &&
          communication.state != Communication::SETTLING)
        return false;
      std::erase_if(rounds_, [&](const auto& r) {
        return std::none_of(durable.outbox.begin(), durable.outbox.end(),
                            [&](const auto& f) { return r.id == f.id; });
      });
      for (const auto& f : durable.outbox) {
        auto it = std::find_if(rounds_.begin(), rounds_.end(),
                               [&](const auto& r) { return r.id == f.id; });
        if (it == rounds_.end()) {
          rounds_.push_back({f.id, now, 0});
          it = rounds_.end() - 1;
        }
        if (now - it->start >= 2500) {
          it->start   = now;
          it->attempt = 0;
        }
        constexpr uint64_t offsets[] = {0, 250, 750, 1750};
        if (it->attempt < 4 && now - it->start >= offsets[it->attempt]) {
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

    /// Process a validated, provisioned ACK or configuration command. Configuration and result
    /// commit together; failed storage leaves the prior configuration active and produces no
    /// success.
    void receive(const Frame& f, uint64_t now) {
      if (!valid(f) || f.id.node != identity.node) return;
      if (f.type == Type::EVENT_ACK) {
        Durable changed = durable;
        std::erase_if(changed.outbox, [&](const auto& event) {
          return event.id.boot == f.ack_boot && event.id.seq == f.ack_seq;
        });
        if (changed.outbox.size() != durable.outbox.size() && commit(changed))
          std::erase_if(rounds_, [&](const auto& r) {
            return r.id.boot == f.ack_boot && r.id.seq == f.ack_seq;
          });
        return;
      }
      if (f.type != Type::CONFIG_SET) return;
      Status result;
      const auto record = std::find_if(durable.commands.begin(), durable.commands.end(),
                                       [&](const auto& c) { return c.uuid == f.command; });
      if (record != durable.commands.end()) {
        result = record->config == f.config ? record->status : Status::command_conflict;
        for (const auto& event : durable.outbox)
          if (event.type == Type::COMMAND_RESULT && event.command == f.command &&
              event.status == result)
            return;
      } else if (f.config.revision < durable.config.revision)
        result = Status::stale_revision;
      else if (f.config.revision == durable.config.revision)
        result = f.config == durable.config ? Status::already_applied : Status::revision_conflict;
      else
        result = Status::applied;
      const auto protectedCount =
          std::count_if(durable.outbox.begin(), durable.outbox.end(),
                        [](const auto& e) { return e.type == Type::COMMAND_RESULT; });
      if (protectedCount >= 31) {
        auto receipt    = frame(Type::COMMAND_RECEIPT, now);
        receipt.command = f.command;
        receipt.status  = Status::busy;
        if (best_.size() < 4) best_.push_back(receipt);
        return;
      }
      Durable changed = durable;
      // A repeated UUID returns its historical result without rolling back newer settings.
      if (record == durable.commands.end() && result == Status::applied) changed.config = f.config;
      if (record == durable.commands.end()) {
        if (changed.commands.size() == 32) changed.commands.erase(changed.commands.begin());
        changed.commands.push_back({f.command, f.config, result});
      }
      auto event            = frame(Type::COMMAND_RESULT, now);
      event.command         = f.command;
      event.status          = result;
      event.common.revision = changed.config.revision;
      reserve(changed, now);
      changed.outbox.push_back(event);
      if (commit(changed)) {
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
        if (best_.size() < 4) best_.push_back(receipt);
      }
    }

   private:
    struct Round {
      Header id;
      uint64_t start;
      unsigned attempt;
    };

    std::vector<Round> rounds_;
    std::deque<Frame> best_;
    std::vector<Sample> batch_;
    Save save_;
    bool statePending_    = false;
    Reason openingReason_ = Reason::scheduled;
    bool seatSeen_ = false, candidate_ = false, candidateMotion_ = false, moving_ = false,
         stopping_   = false;
    uint64_t seatAt_ = 0, motionAt_ = 0, batchAt_ = 0, intervalAt_ = 0, nextSample_ = 0,
             heartbeatAt_ = 0, sleepAt_ = 0, storageRetry_ = 0;

    bool commit(const Durable& d) {
      if (!save_(d)) {
        storageFailed = true;
        return false;
      }
      durable       = d;
      storageFailed = statePending_;
      return true;
    }

    void reserve(Durable& d, uint64_t now) {
      if (d.outbox.size() < 31) return;
      std::erase_if(d.outbox, [](const auto& f) { return f.type != Type::COMMAND_RESULT; });
      auto sync            = frame(Type::STATE_SYNC, now);
      sync.common.revision = d.config.revision;
      d.outbox.push_back(std::move(sync));
    }

    void queueEvent(Type type, uint64_t now) {
      Durable changed = durable;
      reserve(changed, now);
      if (changed.outbox.size() >= 32) {
        // Protected results must survive; replace only the reserved state snapshot.
        std::erase_if(changed.outbox, [](const auto& f) { return f.type == Type::STATE_SYNC; });
        type = Type::STATE_SYNC;
      }
      changed.outbox.push_back(frame(type, now));
      statePending_ = !commit(changed);
      storageFailed = statePending_;
    }

    void append(Sample s, uint64_t now) {
      if (now - batchAt_ > UINT16_MAX || batch_.size() == MAX_SAMPLES) flush(End::capacity, now);
      if (batch_.empty()) batchAt_ = now;
      s.offset = uint16_t(now - batchAt_);
      batch_.push_back(s);
    }

    void flush(End end, uint64_t now) {
      auto f     = frame(Type::MOVEMENT_BATCH, now);
      f.start    = uint32_t(batchAt_);
      f.duration = uint32_t(std::min<uint64_t>(UINT32_MAX, now - batchAt_));
      f.end      = end;
      f.samples  = std::move(batch_);
      batch_.clear();
      batchAt_ = intervalAt_ = now;
      if (best_.size() >= 4) best_.pop_front();
      best_.push_back(std::move(f));
    }
  };
}  // namespace SmartChair
