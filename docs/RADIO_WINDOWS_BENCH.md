# Radio-window recovery and bench tools

## Recovery behavior

Both nodes run Wi-Fi/ESP-NOW start and stop calls in one FreeRTOS worker at a
time. The loop continues sampling the occupied sensor and servicing USB while
that worker runs. The original 2.5-second start/stop timeout still reports a
failure; it does not delete a task that may hold SDK locks.

A worker still outstanding after 15 seconds requests a full emergency restart.
The sensor waits until its initial state, pending occupancy events and boot
identity are durably committed, no storage failure remains, the contact debounce
has settled, and a final GPIO read agrees with the committed occupancy. Failed
storage postpones this restart; state is never erased to force recovery. The
gateway has no durable state and can restart immediately after a short diagnostic
output opportunity. Sensor reliable events remain in its NVS outbox and retry
after reconnecting. RAM-only movement batches may be lost across this reset.

The eight send callback slots remain quarantined after a missing completion.
Exhausting them latches a reboot request rather than reusing an identity that a
late callback could complete incorrectly. A subsequent late callback does not
cancel an already requested reboot.

Emergency recovery uses the pinned ESP-IDF's `esp_restart_noos()` to reset CPUs
and peripherals without Wi-Fi shutdown hooks, which could wait on the stalled
worker's locks. This is deliberately limited to unrecoverable radio faults.
It depends on the pinned IDF internal API; rebuild both targets when upgrading
that dependency. A diagnostic is queued before reset, which waits at least 20 ms
and until the USB queue drains, but never more than 250 ms; USB output is best
effort and delivery is not guaranteed. The sensor also waits for its persistence
gate, but a restart is forced 60 s after the notice so failing storage cannot
keep a radio-less chair awake. Hardware recovery must still be validated on both
ESP models; host tests verify the policy and persistence gate.

HELLO reads a cached snapshot of worker-confirmed settings and the hardware MAC,
so it cannot block on Wi-Fi getters during a lifecycle stall. `lifecycle_busy`
identifies a snapshot taken during an operation. Channel/power settings represent
the last successful initialization, rather than independent live measurements.

Normal firmware retains 30-second occupied windows, five-second opening duration,
ten-second motion tail, 50 ms seat debounce, 25 Hz occupied acquisition and the
existing vacant-only deep sleep. No occupied light sleep was enabled.

## Sensor traces

`platformio.ini` supplies two explicitly selected bench environments:

- `sensor-diagnostics`: adds USB `sensor_trace` messages without an artificial delay.
- `sensor-window-test`: also delays each radio startup by 1800 ms, making a
  confirmed motion stop before readiness easier to reproduce.

The production `sensor` environment enables neither instrumentation nor delay.
Neither bench environment erases NVS or changes the durable configuration.
Restore `sensor` after testing. Do not use the delayed or diagnostic image to
claim production power measurements.

Identify the sensor's stable serial path and flash the desired bench image:

```sh
pio run -e sensor-window-test -t upload --upload-port /dev/serial/by-id/SENSOR
python3 tools/radio_window_bench.py trace \
  --port /dev/serial/by-id/SENSOR --duration 90 --tail-s 10 | tee tail-trace.jsonl
```

Replace `SENSOR` with the actual device entry. Hold the contact occupied, start
movement while a startup is pending, then stop and keep the sensor still. To
coordinate a repeat, leave it still until the radio closes, then move it briefly
to trigger another startup. Traces record raw contact edges, confirmed occupancy,
motion start/stop, radio actions/readiness, boot identity, monotonic time and the
engine's closing deadline. A captured `motion_stop` during `opening`, followed
by `radio_ready` in the same boot, must preserve `stop_ms + tail_s * 1000`.

The analyzer returns exit status 0 for a passing deadline check, 1 for a failing
check and 2 for an incomplete trace. The check verifies the engine's deadline at
actual firmware readiness; it does not independently measure RF activity or
current draw. A USB disconnect leaves a trace incomplete and never establishes
a pass. For contact diagnosis use `sensor-diagnostics`, compare
`raw_occupied` changes with `occupied`, and inspect wiring/contact continuity
with power disconnected. Do not increase debounce to hide an unidentified fault.

Restore production firmware using the sensor port, without erasing flash:

```sh
pio run -e sensor -t upload --upload-port /dev/serial/by-id/SENSOR
```

If BOOT was used for upload, release it and reset into normal execution. Vacant
deep sleep can remove native USB enumeration; hold the contact occupied while
reconnecting for a trace. Keep any gateway powered separately.

## Gateway fault tests

Stop other bridge/monitor processes on the gateway's port. Run these with the
gateway attached and sensor occupied and stationary. Provide the complete
**current** configuration in a JSON file, including its revision. The harness
compares the observed sensor revision before queuing an equal-revision command.
Matching settings produce `already_applied`; a mismatch is inconclusive. It does
not deliberately change settings or increment revisions.

```sh
python3 tools/radio_window_bench.py delayed-ready \
  --port /dev/serial/by-id/GATEWAY --nodes nodes.json --node chair-01 \
  --db /tmp/chair-delayed-ready.sqlite --config /tmp/current-chair-config.json \
  --duration 120 | tee delayed-ready.jsonl

python3 tools/radio_window_bench.py lost-ack \
  --port /dev/serial/by-id/GATEWAY --nodes nodes.json --node chair-01 \
  --db /tmp/chair-lost-ack.sqlite --config /tmp/current-chair-config.json \
  --duration 120 | tee lost-ack.jsonl
```

Each `--db` must be a new path; existing databases are rejected. Reliable events
acknowledged by this harness are durably stored **in this test database**, not in
a production bridge database. Keep it if those facts are needed later.

The delayed-READY test holds a real announcement for its advertised duration plus
one second, then observes three seconds in which probes are allowed but commands
are not. Fresh availability or vacancy makes the controlled scenario inconclusive.

The lost-ACK test withholds the first no-op command result's application ACK,
queues a second no-op solely to request a newer proof, suppresses that second
command, and waits for the old result to retry during the newer lease. A valid
lease must survive that retry. ACK withholding ends when the test finishes;
cleanup submits a fresh ACK best effort and reports whether serial submission
succeeded. Submission does not prove sensor receipt. If cleanup cannot reach the
sensor, its durable result will retry to a normal bridge later.

Structured JSON output includes RX, TX, injected suppression, outcomes and
cleanup. Exit statuses are 0=pass, 1=fail, 2=inconclusive. Unfinished test commands
are expired during cleanup; command history and accepted facts are retained.
The tool uses `Bridge.send()` and all current serial deadline guards.

## Current measurement

The user's UNI-T UT33A+ can measure current in series with a supply connection;
an intact USB cable alone does not expose that connection. A USB breakout or
other appropriate accessible supply wiring is needed before proceeding. Never
place current-mode probes directly across a supply and ground. Switch power off
before changing wiring, sockets or ranges, and follow the
[manufacturer's manual](https://meters.uni-trend.com/download/ut33a-b-c-d-user-manual/).

Use normal production firmware and the same supply, peripherals and board setup
for every comparison. Measure the complete sensor board's occupied stationary
current during radio-open and radio-closed portions, then vacant sleep separately.
Check that measurement burden does not cause resets. A handheld meter's readings
can miss radio bursts; a few displayed values cannot establish a cycle average
or battery-life estimate. For a meaningful average, integrate complete repeated
30-second cycles with suitable logging equipment and compare against an otherwise
identical always-on-radio configuration. Record voltage, configuration, range,
measurement location and instrument limitations with the results. Do not change
sensor configuration for a power comparison without recording and restoring it.

Current savings remain unmeasured until that setup is available.
