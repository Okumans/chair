# Radio window review fixes

Reviewed implementation: `099a334` (`feat: add scheduled radio communication windows`).
Compared against: `f9409a9`, the checkout revision containing
[RADIO_WINDOWS_SPEC.md](RADIO_WINDOWS_SPEC.md).

The implementation covers most of the v2 design, but the following reproduced
cases must be fixed before it can be considered fully compliant. Source line
references below refer to implementation commit `099a334`.

## 1. Preserve the motion tail when stop occurs during startup

Source: [shared/src/chair_engine.cpp:218–219](https://github.com/Okumans/chair/blob/099a334/shared/src/chair_engine.cpp#L218-L219).

The specification requires confirmed stop to preserve availability until at least
`stop_time + radio_motion_tail_s`, including any later deadline already granted.
The implementation applies the tail only when communication is `OPEN`.

Reproduced case with default settings:

1. Confirm occupancy at 50 ms and begin radio startup.
2. Confirm movement at 220 ms while startup is pending.
3. Confirm stop at 380 ms, still during startup.
4. Report radio readiness at 450 ms.

Availability ends at **5450 ms**, instead of at least **10380 ms**. The five-second
opening duration replaces the required stop tail.

Fix: retain the stop deadline across opening and lifecycle retries, then preserve
it when readiness arrives. Add a regression test for this sequence.

If left unchanged, communication can close early after movement.

## 2. Reject READY announcements whose availability has already expired

Source: [tools/protocol_bridge.py:460–464](https://github.com/Okumans/chair/blob/099a334/tools/protocol_bridge.py#L460-L464).

The specification requires the Pi to initiate commands only within the sensor's
advertised availability and states that expired announcements cannot extend a Pi
lease. The bridge instead starts the entire lease at USB receipt.

Reproduced case: a READY advertising five seconds, delivered six seconds after
transmission on an established connection, grants another five seconds. The bridge
dispatches a queued command even though the original availability has expired.
The implementation report also acknowledges this gap.

Fix: account for end-to-end announcement age and reject expired READY messages.
Add a regression test for delayed delivery on an established connection; the
reconnect hello barrier alone does not cover this case.

If left unchanged, the Pi can send commands while the sensor's radio is closed.

## 3. Keep old command-result retries from clearing newer availability

Source: [tools/protocol_bridge.py:435–439](https://github.com/Okumans/chair/blob/099a334/tools/protocol_bridge.py#L435-L439).

The specification requires a successful policy result to invalidate prior
availability and require fresh READY. Every successful result retransmission
currently clears availability, including duplicates older than that fresh READY.

Reproduced sequence within one boot:

1. Receive an applied command result with sequence 2.
2. Receive fresh READY with sequence 3, establishing a valid lease.
3. Receive a duplicate of the command result with sequence 2.

The duplicate removes the valid lease. This can occur normally when the sensor
does not receive the application's ACK and retries its durable result.

Fix: track which READY established the lease and prevent older result retries from
invalidating it. Add a regression test for the sequence above.

If left unchanged, lost ACKs can suppress command delivery during valid windows.

## Validation and remaining acceptance work

- Formatting and lint checks passed.
- All 35 existing tests passed using `uv run --locked python -m unittest discover -s tests -v`.
- The three cases above were reproduced separately against the implementation in
  a temporary copy. Those reproduction checks are not committed regression tests.
- `make check` reached its test step but could not use the default uv cache under
  the sandbox; the test suite was then run directly with the installed Python.
- Firmware builds and physical acceptance were not rerun for this review.

Hardware timing, wake behavior, encrypted delivery after restart, and power savings
remain unverified by this review. Fixes 1–3 are required before claiming full
compliance with the radio window specification.
