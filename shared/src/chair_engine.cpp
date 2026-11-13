#include "chair_engine.h"

#include <algorithm>
#include <cmath>

namespace SmartChair {
  Engine::Engine(Header id, Durable data, Save save)
      : durable(std::move(data)), identity(id), save_(save) {}

  void Engine::radioReady(uint64_t now) {
    if (communication.state != Communication::OPENING) return;
    radioReady_    = true;
    openingWanted_ = false;
    communication.radioReady(now);
    if (occupied && durable.config.radio_windows_enabled)
      communication.closes = std::max(communication.closes, motionTailUntil_);
    rounds_.clear();
    if (occupied) {
      if (motionHold_ && durable.config.radio_windows_enabled)
        communication.closes =
            std::max(communication.closes,
                     deadlineAfter(now, milliseconds(durable.config.radio_motion_tail_s)));
      ready(openingReason_, now);
      openingReason_ = Reason::scheduled;
    } else {
      communication.state    = Communication::SETTLING;
      communication.deadline = deadlineAfter(sleepAt_, Timing::DELIVERY_ROUND_MS);
    }
  }

  void Engine::radioStopped(uint64_t now) {
    if (!stoppingRadio_) return;
    stoppingRadio_      = false;
    shutdownStarted_    = false;
    radioReady_         = false;
    communication.state = Communication::CLOSED;
    openingWanted_      = false;
    if (occupied && reopenAfterStop_) {
      reopenAfterStop_ = false;
      requestOpen(openingReason_, now);
    }
    if (!occupied && operating == Operating::PREPARE_SLEEP && !storageFailed && !candidate_)
      operating = Operating::DEEP_SLEEP;
  }

  void Engine::radioStartFailed(uint64_t now, bool stopped) {
    radioReady_            = false;
    communication.state    = stopped ? Communication::CLOSED : Communication::SETTLING;
    communication.deadline = now;
    reopenAfterStop_       = !stopped && occupied;
    openingWanted_         = true;
    lifecycleRetry_        = deadlineAfter(now, Timing::RADIO_RETRY_MS);
  }

  void Engine::radioStopFailed(uint64_t now, bool stillReady) {
    stoppingRadio_  = false;
    radioReady_     = stillReady;
    lifecycleRetry_ = deadlineAfter(now, Timing::RADIO_RETRY_MS);
  }

  void Engine::radioFault(uint64_t now) {
    if (communication.state == Communication::OPEN ||
        communication.state == Communication::OPENING) {
      communication.state    = Communication::SETTLING;
      communication.deadline = now;
      reopenAfterStop_       = occupied;
      openingReason_         = Reason::scheduled;
      clearReady();
    }
  }

  RadioAction Engine::radioAction(uint64_t now, bool busy) {
    if (now < lifecycleRetry_) return RadioAction::NONE;
    if (communication.state == Communication::CLOSED && openingWanted_ &&
        (occupied || now - sleepAt_ < Timing::DELIVERY_ROUND_MS))
      communication.open(occupied && !durable.config.radio_windows_enabled
                             ? 0
                             : milliseconds(durable.config.radio_open_s));
    if (communication.state == Communication::OPENING) {
      if (!occupied && now - sleepAt_ >= Timing::DELIVERY_ROUND_MS) {
        communication.state    = Communication::SETTLING;
        communication.deadline = deadlineAfter(sleepAt_, Timing::DELIVERY_ROUND_MS);
      } else {
        return RadioAction::START;
      }
    }
    const bool pending = busy || !durable.outbox.empty() || !best_.empty();
    if (communication.state == Communication::SETTLING && !stoppingRadio_ && !busy &&
        (!pending || now >= communication.deadline)) {
      stoppingRadio_   = true;
      shutdownStarted_ = true;
      return RadioAction::STOP;
    }
    return RadioAction::NONE;
  }

  void Engine::requestOpen(Reason reason, uint64_t now) {
    if (!occupied) return;
    openingWanted_ = true;
    if (reason < openingReason_) openingReason_ = reason;
    const auto duration =
        durable.config.radio_windows_enabled ? milliseconds(durable.config.radio_open_s) : 0;
    if (shutdownStarted_ || (communication.state == Communication::SETTLING && !radioReady_)) {
      reopenAfterStop_ = true;
      return;
    }
    if (communication.state == Communication::CLOSED) {
      if (now >= lifecycleRetry_) communication.open(duration);
    } else if (communication.state == Communication::OPENING) {
      communication.extend(now, duration);
    } else {
      communication.extend(now, duration);
      if (motionHold_ && durable.config.radio_windows_enabled)
        communication.closes =
            std::max(communication.closes,
                     deadlineAfter(now, milliseconds(durable.config.radio_motion_tail_s)));
      openingWanted_ = false;
      ready(reason, now);
    }
  }

  void Engine::seat(bool raw, uint64_t now, Wake wake) {
    if (!seatSeen_ || raw != candidate_) {
      seatSeen_  = true;
      candidate_ = raw;
      seatAt_    = now;
    }
    if (now - seatAt_ < Timing::SEAT_DEBOUNCE_MS) return;
    if (operating == Operating::STARTING || occupied != candidate_) {
      const bool starting = operating == Operating::STARTING;
      if (!candidate_ && (moving_ || candidateMotion_)) flush(End::vacant, now);
      occupied = candidate_;
      moving_ = stopping_ = candidateMotion_ = false;
      batch_.clear();
      operating    = occupied ? Operating::OCCUPIED_IDLE : Operating::PREPARE_SLEEP;
      sleepAt_     = now;
      nextSample_  = now;
      heartbeatAt_ = now;
      Type event   = occupancyEvent();
      if (starting) {
        event = startupEvent(wake);
        if (wake == Wake::cold_boot || durable.boot_generation == 0) {
          newBoot_ = true;
          event    = Type::STATE_SYNC;
        }
      }
      queueEvent(event, now);
      if (occupied) {
        motionHold_     = false;
        scheduleActive_ = true;
        nextSchedule_   = deadlineAfter(now, milliseconds(durable.config.radio_interval_s));
        openingReason_  = Reason::seat_occupied;
        requestOpen(Reason::seat_occupied, now);
      } else {
        scheduleActive_ = motionHold_ = false;
        motionTailUntil_              = 0;
        reopenAfterStop_              = false;
        clearReady();
        openingWanted_ = communication.state == Communication::CLOSED;
        if (communication.state == Communication::OPEN) communication.settle(now);
        if (communication.state == Communication::SETTLING)
          communication.deadline = deadlineAfter(now, Timing::DELIVERY_ROUND_MS);
      }
      rounds_.clear();
    }
  }

  void Engine::sampleFailed(uint64_t now) {
    imuAvailable = false;
    nextSample_  = deadlineAfter(now, Timing::SAMPLE_INTERVAL_MS);
    if (moving_) {
      // A sensor outage is not evidence that the stop debounce stayed quiet.
      stopping_ = false;
      operating = Operating::MOVING;
    } else if (candidateMotion_) {
      candidateMotion_ = false;
      batch_.clear();
      if (occupied) operating = Operating::OCCUPIED_IDLE;
    }
  }

  void Engine::sample(Sample raw, uint64_t now) {
    nextSample_ = deadlineAfter(now, Timing::SAMPLE_INTERVAL_MS);
    if (!occupied || !imuAvailable || !durable.config.movement_enabled) return;
    const auto motion = classifyMotion(raw);

    if (!moving_) {
      operating = Operating::SAMPLE_CHECK;
      if (motion != Motion::active) {
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
      if (now - motionAt_ < Timing::MOTION_DEBOUNCE_MS) return;
      moving_          = true;
      candidateMotion_ = false;
      stopping_        = false;
      operating        = Operating::MOVING;
      motionHold_      = true;
      requestOpen(Reason::movement_started, now);
    } else {
      append(raw, now);
      if (motion == Motion::quiet) {
        if (!stopping_) {
          stopping_ = true;
          motionAt_ = now;
        }
        operating = Operating::MOTION_STOP_CHECK;
        if (now - motionAt_ >= Timing::MOTION_DEBOUNCE_MS) {
          flush(End::stopped, now);
          moving_ = stopping_ = false;
          motionHold_         = false;
          if (durable.config.radio_windows_enabled) {
            motionTailUntil_ =
                std::max(motionTailUntil_,
                         deadlineAfter(now, milliseconds(durable.config.radio_motion_tail_s)));
          }
          if (communication.state == Communication::OPEN && durable.config.radio_windows_enabled)
            communication.extend(now, milliseconds(durable.config.radio_motion_tail_s));
          operating = Operating::OCCUPIED_IDLE;
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

  void Engine::tick(uint64_t now, bool radioBusy) {
    if (durable.outbox.empty())
      delivery = Delivery::IDLE;
    else if (communication.state != Communication::OPEN &&
             communication.state != Communication::SETTLING)
      delivery = Delivery::WAITING;
    if (occupied && now - heartbeatAt_ >= milliseconds(durable.config.heartbeat_s)) {
      heartbeatAt_ = now;
      queueEvent(Type::HEARTBEAT, now);
    }
    if (statePending_ && now >= storageRetry_) {
      storageRetry_ = deadlineAfter(now, Timing::STORAGE_RETRY_MS);
      persistPending(now);
    }
    if (occupied && scheduleActive_ && durable.config.radio_windows_enabled &&
        now >= nextSchedule_) {
      const uint64_t interval = milliseconds(durable.config.radio_interval_s);
      const uint64_t steps    = (now - nextSchedule_) / interval + 1;
      if (steps > (UINT64_MAX - nextSchedule_) / interval)
        scheduleActive_ = false;
      else
        nextSchedule_ += steps * interval;
      requestOpen(Reason::scheduled, now);
    }
    if (occupied && motionHold_ && communication.state == Communication::OPEN &&
        durable.config.radio_windows_enabled) {
      communication.closes =
          std::max(communication.closes,
                   deadlineAfter(now, milliseconds(durable.config.radio_motion_tail_s)));
      if (readyRefresh_ && now >= readyRefresh_) ready(Reason::scheduled, now);
    }
    communication.tick(now, radioBusy || !durable.outbox.empty() || !best_.empty());
    if (communication.state != Communication::OPEN) clearReady();
    if (operating == Operating::PREPARE_SLEEP && communication.state == Communication::CLOSED &&
        now - sleepAt_ >= Timing::DELIVERY_ROUND_MS)
      openingWanted_ = false;
    if (operating == Operating::OCCUPIED_IDLE && canOccupiedSleep() && now < nextSample_)
      operating = Operating::OCCUPIED_LIGHT_SLEEP;
    if (operating == Operating::OCCUPIED_LIGHT_SLEEP && now >= nextSample_)
      operating = Operating::SAMPLE_CHECK;
    if (operating == Operating::PREPARE_SLEEP && !storageFailed && !candidate_ &&
        communication.state == Communication::CLOSED && !openingWanted_ && !stoppingRadio_ &&
        !radioReady_)
      operating = Operating::DEEP_SLEEP;
  }

  Engine::Motion Engine::classifyMotion(const Sample& raw) {
    const double magnitude =
        std::sqrt(double(raw.ax) * raw.ax + double(raw.ay) * raw.ay + double(raw.az) * raw.az);
    const double accelerationDeviation =
        std::abs(magnitude / MotionThresholds::ACCEL_COUNTS_PER_G - 1.0);
    const int largestGyroAxis =
        std::max({std::abs(int(raw.gx)), std::abs(int(raw.gy)), std::abs(int(raw.gz))});
    const double angularSpeed = largestGyroAxis / MotionThresholds::GYRO_COUNTS_PER_DPS;

    if (accelerationDeviation > MotionThresholds::START_ACCEL_G ||
        angularSpeed > MotionThresholds::START_GYRO_DPS)
      return Motion::active;
    if (accelerationDeviation < MotionThresholds::QUIET_ACCEL_G &&
        angularSpeed < MotionThresholds::QUIET_GYRO_DPS)
      return Motion::quiet;
    return Motion::intermediate;
  }

  void Engine::append(Sample s, uint64_t now) {
    if (now - batchAt_ > UINT16_MAX || batch_.size() == MAX_SAMPLES) flush(End::capacity, now);
    if (batch_.empty()) batchAt_ = now;
    s.offset = uint16_t(now - batchAt_);
    batch_.push_back(s);
  }

  void Engine::flush(End end, uint64_t now) {
    auto f = frame(Type::MOVEMENT_BATCH, now);
    if (!f.id.seq) {
      if (droppedBatches_ != UINT32_MAX) ++droppedBatches_;
      batch_.clear();
      batchAt_ = intervalAt_ = now;
      return;
    }
    f.start    = uint32_t(batchAt_);
    f.duration = uint32_t(std::min<uint64_t>(UINT32_MAX, now - batchAt_));
    f.end      = end;
    f.samples  = std::move(batch_);
    batch_.clear();
    batchAt_ = intervalAt_ = now;
    if (best_.size() >= Limits::BEST_EFFORT_QUEUE) discardBest();
    best_.push_back(std::move(f));
  }
}  // namespace SmartChair
