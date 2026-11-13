# Radio-window physical test record

Tested the `fix/radio-window-review` firmware from commit `dadfcc1` with the
ESP32-C3 sensor (`48:f6:ee:12:a0:d8`), ESP32-S3 gateway
(`80:b5:4e:c3:fb:18`), and USB bridge, with the user's assistance.

## Results

| Check | Result | Observation and limits |
| --- | --- | --- |
| Periodic occupied availability | Passed | Gateway READY receipt intervals of approximately 30.005 and 30.004 seconds. This measures announcements, not radio current. |
| Command round trip | Passed | A correlated READY_PROOF preceded a default-value configuration command and an `already_applied` result. |
| Delayed READY | Passed for the injected case | A real READY was held for its advertised duration plus one second. The bridge sent a probe but no configuration command during the subsequent three-second observation. |
| Old result retry after newer availability | Passed | With the application ACK for command-result sequence 65 withheld, the sensor later sent READY 67 and READY_PROOF 68. A subsequent duplicate of result 65 preserved the newer lease. The harness then submitted the withheld ACK. |
| Vacancy and seat wake | Passed for the observed sequence | Release produced VACATED; pressing again produced OCCUPIED and READY with a fresh low uptime. Current consumption and wake latency were not measured. |
| Vacant timer wake | Passed for the shortened test interval | Temporarily setting heartbeat to 15 seconds produced repeated vacant HEARTBEAT events about 15.1 seconds apart, with low post-wake uptimes. |
| Motion stops during startup | Inconclusive on hardware | An isolated test firmware added a 1.8-second startup delay and timestamp diagnostics. USB/WSL disconnects prevented capture of a valid stop-during-startup trace. Automated regression tests cover this case. |
| Power savings and exact sample timing | Not measured | Requires suitable current-measurement and timing equipment. |

## Test controls and cleanup

- Test bridge facts and commands were recorded in the temporary SQLite database
  `/tmp/chair-fixes-test.Ap2o4K/chair.sqlite`. Reliable events acknowledged during
  testing are stored there, rather than in a production Pi database.
- Temporary delayed-READY and lost-ACK harnesses ran outside the repository.
- The timer test applied configuration revision 1 with `heartbeat_s=15`. A
  successful revision-2 result subsequently confirmed restoration to
  `heartbeat_s=900`, with the other default settings retained.
- The temporary startup-delay firmware was replaced with the normal repository
  sensor firmware. The upload completed with its written hash verified, followed
  by a full software reset to leave download mode.
- No sensor-state reset or existing Pi database deletion was performed during
  this test session.

Local evidence includes `physical-check.log`, `wake-check.log`, `timer-check.log`,
`tail-capture.log`, and the test database under `/tmp/chair-fixes-test.Ap2o4K/`.
Those files are temporary and are not part of this repository. The delayed-READY
and lost-ACK pass outputs were also observed in the test session.

## Follow-up

Rapid occupied/vacant transitions appeared around pressure-contact handling.
The source has not been determined; check the contact and its wiring before
relying on stable occupancy measurements. A stable connection and repeatable
motion fixture are needed to complete the startup-tail physical test.

These results do not establish exhaustive radio reliability, current savings,
precise motion-tail closure, or recovery from indefinitely stuck Wi-Fi calls.

## Recovery and diagnostics follow-up

The follow-up source adds asynchronous gateway lifecycle work, guarded emergency
reset, named bridge records and committed bench tools. `make check` passed
formatting, lint and 48 tests, including stalled workers, callback-slot exhaustion,
continued USB work, durable-state gating, contact bounce before reset and the
bench-tool scenarios. Sensor, gateway and both diagnostic variants were built.
These automated checks do not establish the low-level reset's behavior under an
actual SDK deadlock on either board.

The identified ESP32-C3 was flashed with `sensor-window-test`, containing the
1800 ms artificial startup delay. The written hashes verified, and a watchdog
reset left download mode. A live USB capture showed a ready encrypted peer on
channel 1 and successful MAC send completions to the powered gateway. This is
MAC delivery evidence; the gateway's USB bridge was not running for application
ACK/command testing in this capture.

While the user reported holding the seat contact, diagnostic samples recorded:

| Sensor uptime (ms) | Observation |
| --- | --- |
| 69,373 | Radio confirmed closed while occupied and stationary. |
| 83,773 | Raw contact changed to released; debounced state still occupied. |
| 83,824 | Debounced vacancy, 51 ms after the observed raw release. |
| 84,166 | Raw contact changed to occupied. |
| 84,216 | Debounced occupancy, 50 ms later. |
| 84,684 | Raw contact changed to released again. |
| 84,735 | Debounced vacancy, 51 ms later. |
| 85,634 | Radio startup completed while vacant; bounded settling applied. |
| 87,235 | Radio shutdown requested; USB disconnected immediately afterward. |

No complete confirmed motion-stop-during-startup sequence was captured, so that
hardware acceptance case remains **inconclusive**. These raw transitions explain
why debounce accepted the observed state changes; they do not distinguish a
contact fault, wiring fault, insufficient pressure or electrical interference.
The 50 ms debounce and sleep behavior were preserved.

The temporary delay image was replaced with production `sensor` firmware without
an NVS erase. Local capture and restoration evidence is in
`/tmp/chair-improvements-tail.jsonl` and `/tmp/chair-improvements-restore.log`.
The final software/build checks are in `/tmp/chair-improvements-check.log` and
`/tmp/chair-improvements-build.log`. These paths are local working-session files,
not repository artifacts. The new gateway firmware has not yet been flashed in
this follow-up session.

The user has a UNI-T UT33A+ and an intact USB cable, without a breakout or
accessible supply connection. Current was not measured. The
[bench guide](RADIO_WINDOWS_BENCH.md) explains the required setup and limitations.
