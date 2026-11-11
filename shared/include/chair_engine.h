#pragma once

#include <deque>

#include "chair_storage.h"
#include "communication_window.h"

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
  /// Hardware wake cause selects startup reporting without inventing cold-boot transitions.
  enum class Wake { cold_boot, seat, timer };

  /// Observable outbox status; MAC delivery never substitutes for a durable Pi ACK.
  enum class Delivery { IDLE, RETRYING, WAITING };

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
    uint32_t occupied_sleep_ms = 0;
    // Internal future-window policy only; no hardware schedule or CONFIG_SET field is enabled.
    uint64_t movement_window_ms = 0;
    Header identity;

    /// Restore durable work under the current wake identity and request startup communication.
    Engine(Header id, Durable data, Save save);

    /// Allocate an event identity and snapshot. The adapter rotates boot before sequence
    /// exhaustion.
    Frame frame(Type type, uint64_t now);

    /// Start a fresh nonzero boot identity before sequence exhaustion and persist STATE_SYNC.
    /// Existing outbox events keep their original identities; the adapter supplies the random ID.
    void rotateBoot(uint64_t boot, uint64_t now);

    /// Complete radio opening and announce occupied availability with a fresh READY.
    void radioReady(uint64_t now);

    /// Queue fresh RAM-only availability. Remaining listen time is filled when next() transmits it.
    void ready(Reason reason, uint64_t now);

    /// Tell the adapter when the next 40 ms occupied sample is due, without catch-up spinning.
    bool sampleDue(uint64_t now) const { return occupied && now >= nextSample_; }

    /// Apply a raw seat sample with 50 ms debounce; wake selects sync, seat or heartbeat reporting.
    void seat(bool raw, uint64_t now, Wake wake = Wake::cold_boot);

    /// Classify one raw acquisition, retain debounce samples and flush bounded movement batches.
    /// now is the actual acquisition time in monotonic milliseconds; raw axis values stay
    /// unchanged.
    void sample(Sample raw, uint64_t now);

    /// Retry failed state persistence, queue heartbeats and advance bounded vacant sleep.
    void tick(uint64_t now, bool radioBusy = false);

    /// Allow future occupied sleep only with a nonzero duration, quiet motion and a closed radio.
    bool canOccupiedSleep() const {
      return occupied_sleep_ms && occupied && !moving_ && communication.allowsSleep();
    }

    /// Take one due radio frame without waiting. Reliable data stays in the outbox until exact ACK.
    /// Call only when the radio can accept a send; best-effort frames are consumed when returned.
    bool next(Frame& output, uint64_t now);

    /// Process a validated, provisioned ACK or configuration command. Configuration and result
    /// commit together; failed storage leaves the prior configuration active and produces no
    /// success.
    void receive(const Frame& f, uint64_t now);

   private:
    constexpr Type occupancyEvent() const { return occupied ? Type::OCCUPIED : Type::VACATED; }

    constexpr Type startupEvent(Wake wake) const {
      if (wake == Wake::timer) return Type::HEARTBEAT;
      if (wake == Wake::seat && occupied) return Type::OCCUPIED;
      return Type::STATE_SYNC;
    }

    enum class Motion { quiet, intermediate, active };

    // Convert to physical units only for classification; preserve the original raw sample for
    // reports.
    static Motion classifyMotion(const Sample& raw);

    // The ACK's own boot/sequence identify the control, not the event being removed.
    static constexpr bool matchesAck(const Header& event, const Frame& ack) {
      return event.boot == ack.ack_boot && event.seq == ack.ack_seq;
    }

    void acknowledge(const Frame& ack);

    const CommandRecord* findCommand(const Uuid& uuid) const;

    Status commandResult(const Frame& command, const CommandRecord* previous) const;

    bool resultQueued(const Uuid& uuid, Status status) const;

    bool hasCommandCapacity() const;

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

    bool commit(const Durable& d);

    void reserve(Durable& d, uint64_t now);

    void queueEvent(Type type, uint64_t now);

    void append(Sample s, uint64_t now);

    void flush(End end, uint64_t now);
  };
}  // namespace SmartChair
