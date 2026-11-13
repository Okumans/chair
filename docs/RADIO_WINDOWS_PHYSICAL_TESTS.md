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
