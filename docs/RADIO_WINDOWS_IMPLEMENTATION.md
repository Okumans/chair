# Radio windows implementation report

This document records the implementation and subsequent review fixes for
[RADIO_WINDOWS_SPEC.md](RADIO_WINDOWS_SPEC.md) and the changes to the
[protocol](PROTOCOL_SPEC.md). It is a review and handoff record; physical acceptance limits are listed below.

## Behavior

- While occupied, the sensor requests a radio opening every 30 seconds by default.
  Each scheduled window grants five seconds of availability from radio readiness.
- Confirmed movement holds communication open. After confirmed movement stops, a
  ten-second tail remains. Delivery can use a bounded settling period after availability
  expires.
- The sensor continues sampling at 25 Hz while occupied, including while its radio is
  off. An IMU outage does not count as confirmed quiet time.
- The gateway normally keeps its radio available continuously.
- Vacant operation retains the existing deep-sleep flow, including its single 2,500 ms
  preparation budget. Radio startup consumes part of that budget. No additional
  communication wake timer or occupied light sleep was added.
- Movement batches use bounded best-effort storage. `MOVEMENT_LOSS` reports batches
  dropped from the RAM queue; the report itself is best effort and can also be lost.

## Changes by component

### Scheduling and engine

The cooperative engine in `shared/src/chair_engine.cpp`,
`shared/src/communication_window.cpp`, and `shared/include/chair_engine.h` now:

- Tracks opening, availability, settling, and confirmed closure independently from
  physical occupancy and movement.
- Advances the periodic schedule without replaying missed windows back-to-back.
- Holds availability for confirmed movement and the configured tail.
- Requests radio start and stop and accepts readiness, completion, and failure feedback.
- Creates fresh RAM-only READY announcements and coalesces or discards obsolete ones.
- Applies radio policy changes through the existing configuration command.
- Preserves the existing seat debounce, sensing, and vacant sleep decisions.

### Delivery and persistence

`shared/src/chair_delivery.cpp` and the engine preserve reliable event retries and
acknowledgment-before-removal. They also:

- Retry physical transitions that temporarily could not be persisted, using a bounded
  RAM queue. Overflow can coalesce history into a current-state snapshot.
- Protect queued command results and reserve capacity for state recovery.
- Guard sequence exhaustion and boot identity rotation against reuse of persisted event
  identities.
- Count discarded movement batches for best-effort loss reporting.

### Radio and firmware lifecycle

`shared/src/radio_transport.cpp`, `sensor/src/main.cpp`, and `gateway/src/main.cpp`:

- Separate USB setup from radio start and stop so USB remains usable across radio cycles.
- Run both nodes' radio lifecycle operations in a worker, allowing sensing and USB work to continue.
- Report startup timeouts and space failed lifecycle retries at least one second apart.
- Clean up partially initialized hardware and restore the station, channel, power settings,
  keys, encrypted peer, and callbacks on restart.
- Quarantine callback slots after timed-out sends so a late callback cannot complete a
  newer transmission.
- Retry gateway radio failures while keeping the gateway normally available.
- Request a durable-state-gated emergency reboot after a 15-second stuck worker or callback-slot
  exhaustion. HELLO uses cached settings so it never takes Wi-Fi locks from the loop.
- Require confirmed shutdown and safe persistence before vacant sleep, then recheck the
  physical seat contact.

### Protocol and configuration

`shared/include/protocol.h`, `shared/include/protocol_json.h`, and related codecs:

- Use wire version 2 and reject version 1 frames.
- Add mandatory fields to the complete `CONFIG_SET` payload:

  | Field | Default | Validation |
  | --- | --- | --- |
  | `radio_windows_enabled` | `true` | Boolean |
  | `radio_interval_s` | `30` | Nonzero uint32 |
  | `radio_open_s` | `5` | Nonzero uint32, no greater than interval |
  | `radio_motion_tail_s` | `10` | Nonzero uint32 |

- Add the best-effort `MOVEMENT_LOSS` telemetry type.
- Require the relevant v2 JSON fields, including a nonzero generation for STATE_SYNC.
- Encode sensor state and configuration in a new storage format. Older or unsupported
  sensor state is rejected instead of silently erased or migrated.

### Pi bridge

`chair_mqtt/protocol_bridge.py`:

- Validates v2 messages and complete radio configuration; exposes radio settings in the
  configuration CLI.
- Uses a new SQLite schema version and rejects incompatible existing databases without
  automatically deleting them.
- Measures READY lease expiry with a monotonic clock and clears previous availability
  after serial reconnect or a successful configuration result.
- Uses a request-correlated gateway hello after reconnect and requires a fresh READY before
  dispatching another command.
- Preserves durable commit before reliable-event ACK, UUID deduplication, command expiry,
  and recording of late command results.
- Prevents best-effort movement and loss messages from incorrectly invalidating a fresh
  READY merely because of their sequence order.

### Reset and documentation

- `tools/reset_sensor/main.cpp` and the `reset-sensor` PlatformIO environment provide an
  explicit provisioning utility. It requires the serial line `RESET-CHAIR-V2` and does
  not run during ordinary startup.
- `docs/TRANSPORT.md` documents the breaking upgrade and reset procedure, acceptance
  boundary, and known recovery limits. `docs/PROTOCOL_SPEC.md` and `README.md` describe
  the v2 protocol and schedule.
- `Makefile` includes the reset utility in formatting and compilation-database generation.

## Validation completed

- Initial implementation: `make check` passed formatting, lint, and 35 tests.
  Subsequent verification is recorded in the follow-up reports below.
- PlatformIO builds passed for `sensor`, `gateway`, and `reset-sensor`.
- Affected clangd translation-unit checks and `git diff --check` passed.
- Tests cover schedule and motion behavior, radio lifecycle failures, vacant sleep,
  persistence and sequence exhaustion, protocol validation, leases and reconnects,
  loss reporting, and explicit reset confirmation.

The gateway firmware was flashed to the connected ESP32-S3 and its USB hello and
malformed-transmit rejection were checked. The first startup correctly rejected empty
pairing keys. Two independent keys were then generated in the ignored local pairing file,
and the gateway was rebuilt and flashed again. Its hello reported the expected gateway
and sensor MAC addresses, channel 1, and radio readiness. The ESP32-C3 sensor was later
connected, identified by its MAC, and flashed with matching keys; its flash hash verified.
After a normal restart, its USB hello responded and reported
`unsupported_or_corrupt_storage_explicit_v2_reset_required`. The user approved an
explicit sensor-state reset; the reset utility reported that the chair state was erased.
After a power cycle, the normal firmware was reflashed and its hash verified. With the
seat contact held occupied, sensor-side USB diagnostics showed its radio close after the
initial opening, reopen approximately 30 seconds later, and close again. ESP-NOW send
callbacks reported many successful MAC deliveries and a few failures. The gateway was
then connected to a bridge using a temporary database: it received occupancy, vacancy,
scheduled READY, and movement messages, and reported successful MAC delivery for 31
bridge transmissions. A previously acknowledged occupied event did not recur at the next
scheduled window. Movement batches included a stopped batch. The temporary SQLite
database passed its integrity check. No existing Pi database was erased.

Rapid occupied/vacant transitions were observed around pressure-sensor handling; their
cause has not been determined and the contact and wiring should be checked. The test did
not precisely measure sample timing, tail closure, sleep current, or radio power use.

These checks do not establish physical radio reliability, sample timing, wake behavior,
or power savings. Both boards must be observable and operating for radio acceptance tests;
see [SETUP.md](SETUP.md#provision-the-boards).

## Known gaps and review items

1. **Physical acceptance:** complete repeatable motion-tail and precise sample-timing checks,
   encrypted recovery after injected hardware faults, and current measurements on both boards.
2. **Pressure contact:** raw input releases lasting longer than the existing 50 ms debounce
   have been captured. Check the contact and its wiring before changing debounce.
3. **Recovery limits:** emergency reset preserves durable events and configuration, but may
   discard RAM-only movement. Failed storage or an unsettled contact postpones sensor reset.
   The low-level reset depends on the pinned IDF internal API and needs hardware fault testing.

The review fixes add a probe and proof exchange to bound READY age across both radio and
USB, preserve the motion tail through startup, and ignore old command-result retries when
they precede a newer lease. The initial review checks passed 38 tests, and both normal
firmware targets build. Both boards were subsequently flashed without resetting their
stored state. A bridge using a separate temporary database received a correlated
READY_PROOF and a successful `already_applied` result for a configuration command using
the existing default values; the temporary database passed its integrity check. The
subsequent physical delayed-READY, lost-ACK, and wake tests are recorded in
[the physical test report](RADIO_WINDOWS_PHYSICAL_TESTS.md). The startup-tail hardware
case remained inconclusive.

A further review fixes deadline checks after SQLite work and before USB writes, and
preserves pending proofs during routine READY refreshes and queue overflow. These
follow-up changes are covered by automated tests and firmware builds; the recorded
physical observations used the earlier `dadfcc1` firmware.

The next improvement adds shared asynchronous lifecycle handling on both nodes, a guarded
emergency restart, named bridge availability/probe/dispatch records, optional contact and
motion traces, and a reusable fault-injection CLI. See
[RADIO_WINDOWS_BENCH.md](RADIO_WINDOWS_BENCH.md) for behavior, test commands and limits.
Normal timing and sleep policy are unchanged. The new physical trace observed confirmed
vacancy about 51 ms after a raw release; the startup-tail sequence was interrupted by
vacant sleep and remains inconclusive. The production sensor image was restored without
resetting stored state after that test. See the physical report for the precise validation
boundary and restoration status.

## Upgrade boundary

Protocol v2 is a breaking upgrade. Follow the explicit procedure in
[TRANSPORT.md](TRANSPORT.md#explicit-v2-test-reset), upgrading the sensor, gateway,
and bridge together. The procedure discards disposable sensor state and requires a new
or explicitly cleared disposable Pi database; it must be performed deliberately.
