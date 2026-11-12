#include "chair_engine.h"

#include <algorithm>
#include <cmath>

namespace SmartChair {
  Engine::Engine(Header id, Durable data, Save save)
      : durable(std::move(data)), identity(id), save_(save) {
    communication.open();
  }

  void Engine::radioReady(uint64_t now) {
    const bool opening = communication.state == Communication::OPENING;
    communication.radioReady(now);
    if (opening && occupied) {
      ready(openingReason_, now);
      openingReason_ = Reason::scheduled;
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
      if (!candidate_ && moving_) flush(End::vacant, now);
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
        openingReason_ = Reason::seat_occupied;
        communication.open();
      } else
        communication.settle(now);
      rounds_.clear();
    }
  }

  void Engine::sampleFailed(uint64_t now) {
    imuAvailable = false;
    nextSample_  = now + Timing::SAMPLE_INTERVAL_MS;
  }

  void Engine::sample(Sample raw, uint64_t now) {
    nextSample_ = now + Timing::SAMPLE_INTERVAL_MS;
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
      communication.extend(now, movement_window_ms);
      if (communication.state == Communication::OPEN)
        ready(Reason::movement_started, now);
      else
        openingReason_ = Reason::movement_started;
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

  void Engine::tick(uint64_t now, bool radioBusy) {
    if (statePending_ && now >= storageRetry_) {
      storageRetry_ = now + Timing::STORAGE_RETRY_MS;
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
        ((!radioBusy && durable.outbox.empty() && best_.empty()) ||
         now - sleepAt_ >= Timing::DELIVERY_ROUND_MS))
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
    auto f     = frame(Type::MOVEMENT_BATCH, now);
    f.start    = uint32_t(batchAt_);
    f.duration = uint32_t(std::min<uint64_t>(UINT32_MAX, now - batchAt_));
    f.end      = end;
    f.samples  = std::move(batch_);
    batch_.clear();
    batchAt_ = intervalAt_ = now;
    if (best_.size() >= Limits::BEST_EFFORT_QUEUE) best_.pop_front();
    best_.push_back(std::move(f));
  }
}  // namespace SmartChair
