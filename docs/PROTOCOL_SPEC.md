# Smart Chair protocol specification

Revision: 0.1.2 draft, 11 November 2026

Draft contract; defaults are proposed and not yet implemented.

## 1. Architecture and responsibilities

```text
Chair node <-- ESP-NOW --> ESP gateway <-- USB --> Raspberry Pi bridge <-- MQTT --> broker / application
```

- Chair: debounce the seat contact, maintain physical occupancy, aggregate movement, report health, apply configuration, and acknowledge commands.
- ESP gateway: validate and forward packets between radio and USB. It does not connect to MQTT or decide session behavior.
- Pi bridge: run as a systemd service, validate USB messages, persist reliable events, publish MQTT, and queue commands.
- Pi application: own sessions and their storage; issue device configuration commands through MQTT.

USB uses bidirectional CDC ACM serial, subject to confirmation of the S3 connector/peripheral. Identify the device by a stable identity rather than a fixed `/dev/ttyACM0` path.

## 2. Physical state and application sessions

Occupancy is `occupied` or `vacant` from the debounced contact. Use 50 ms debounce for both edges and stabilize the initial reading before reporting it. On boot, report `STATE_SYNC` rather than inventing a transition.

Every telemetry message includes current occupancy. Neither ESP stores session state or handles session commands. The Pi application decides session boundaries, absence tolerance, and movement attribution. Ending a session does not change chair sensing or power behavior.

## 3. Identity, ordering, and time

| Field             | Proposed meaning                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| `version`         | Protocol version, initially `1`                                                                                  |
| `node_id`         | Provisioned name, 1–15 ASCII letters, digits, `_` or `-`; unique within the installation                         |
| `boot_id`         | 64-bit random identifier created on cold boot, encoded as 16 hex characters in JSON; retained through deep sleep |
| `seq`             | Unsigned 32-bit event counter within a boot ID; retained through deep sleep                                      |
| `uptime_ms`       | Milliseconds since the current wake, unsigned 32-bit; diagnostic only                                            |
| `config_revision` | Last successfully persisted configuration revision                                                               |

An event's identity is `(node_id, boot_id, seq)`. Retransmission preserves the identity and payload. New events increment `seq`; start a new boot ID before sequence wrap. Delayed events from an earlier boot must not overwrite newer reconciled state.

The Pi adds UTC `received_at` on first acceptance and preserves it across retries. Reception time and arrival order may differ from event occurrence time and order.

## 4. Chair-to-Pi messages

All telemetry carries the common identity fields, `occupancy`, `battery_mv`, and `config_revision`. Battery voltage is an integer in millivolts or `null` when unavailable; never use zero to mean unknown.

| Type             | Trigger / additional payload                                                    | Delivery    |
| ---------------- | ------------------------------------------------------------------------------- | ----------- |
| `OCCUPIED`       | Stable seat closure                                                             | Reliable    |
| `VACATED`        | Stable seat release                                                             | Reliable    |
| `STATE_SYNC`     | Cold boot, reconnect, or recovery; includes `reason` and any known event loss   | Reliable    |
| `MOVEMENT_BATCH` | Completed sampling window: `window_ms`, `motion_count`, `motion_active_ms`      | Best effort |
| `HEARTBEAT`      | Scheduled wake/status; includes wake reason and pending/lost event counts       | Best effort |
| `BATTERY_LOW`    | Measured voltage crosses a configured threshold                                 | Reliable    |
| `COMMAND_RESULT` | Command applied, rejected, or already applied; includes `command_id` and result | Reliable    |

A motion episode begins after activity exceeds a threshold for the configured debounce interval; activity must fall below the threshold before another episode. Units, filtering, and thresholds await IMU selection.

Low-battery detection requires valid measurements and hysteresis; thresholds await battery testing.

## 5. Reliable delivery and acknowledgments

Only reliable messages receive `EVENT_ACK`; best-effort messages have no ACK or retry. Radio send success is not Pi acceptance.

1. The chair assigns an event identity and retains the reliable event in an outbox.
2. The gateway forwards it to the Pi over USB.
3. The Pi validates and commits it to a durable local outbox before returning `EVENT_ACK` through USB and ESP-NOW.
4. The chair removes the acknowledged event from its outbox.
5. The Pi publishes it with MQTT QoS 1 and removes it from its outbox after broker acknowledgment.

`EVENT_ACK` identifies the exact event. Acknowledge duplicates again without creating another logical event. Delivery is at least once; consumers must deduplicate MQTT redelivery too.

Chair retries at 0, 250, 750, and 1750 ms with an ACK deadline of 2500 ms. If unacknowledged, keep the event pending and retry on the next wake or contact event.

The chair persists up to 32 reliable messages before transmission, surviving deep sleep and ordinary resets. On overflow, preserve unacknowledged command results, coalesce older occupancy history into the latest `STATE_SYNC` where possible, and report a loss counter. Overflow can lose history; retention priorities remain to be finalized.

The Pi must not ACK when durable storage is full or unavailable. The gateway uses a bounded transient queue; the chair retries dropped reliable events.

## 6. Downstream commands

Commands follow MQTT → Pi durable queue → USB → gateway → ESP-NOW → chair. Delivery requires the chair to be awake and receiving; this revision defines no downstream communication-window schedule or latency guarantee.

Commands include `version`, `node_id`, a globally unique `command_id`, and UTC `expires_at`. The Pi rejects expired commands and stops retries; the chair requires no wall clock.

| Command      | Proposed behavior                                                                                                     |
| ------------ | --------------------------------------------------------------------------------------------------------------------- |
| `CONFIG_SET` | Apply a complete validated configuration with a monotonically increasing `revision`; persist before reporting success |

`CONFIG_SET` supports `heartbeat_s`, `movement_batch_s` (both in seconds), and movement enable/disable. Motion thresholds await IMU selection. Pin assignments and firmware updates are excluded.

Validate configuration as one transaction: reject the whole command on missing, unknown, or invalid fields. Proposed bounds: heartbeat 60–86400 seconds; movement batch 1–3600 seconds and no longer than heartbeat. Reject a lower revision. The same revision with identical values returns `already_applied`; different values at the same revision return `revision_conflict`.

Persist configuration and command-result state before success so retries remain idempotent across resets.

Command statuses: `queued`, `delivered`, `applied`, `rejected`, `expired`. `delivered` requires chair receipt; `applied`/`rejected` require `COMMAND_RESULT`. Final statuses never regress. Allow one in-flight command per node; deduplicate `command_id` and reject reuse with different content.

## 7. Sleep and heartbeat

The chair may sleep while vacant or occupied, waking for supported contact/motion events and a heartbeat timer. Verify hardware wake support for both contact transitions. Default heartbeat: 900 seconds.

On seat release, attempt the pending movement batch and reliable `VACATED` event before sleeping. Keep unacknowledged reliable events pending without remaining awake indefinitely.

## 8. ESP-NOW wire format

Use compact binary messages with a fixed header and type-specific payloads, limited to 250 bytes per frame without fragmentation.

The header contains `version`, `type`, `payload_length`, `node_id`, `boot_id`, and `seq`. Integer fields use explicitly encoded little-endian bytes. Occupancy, missing battery, message types, and command results need fixed numeric codes.

Define byte offsets, widths, and optional-value encodings before implementation; native C++ struct layout is not the wire contract. `shared/include/protocol.h` does not yet implement this draft.

Reject unsupported versions, unknown types, invalid lengths, invalid enum values, or identities inconsistent with the provisioned sender MAC. Use provisioned unicast peers and radio encryption; peer keys and node assignments are installation configuration, never telemetry. Discovery/pairing UI is outside v1.

## 9. USB framing

Use UTF-8 newline-delimited JSON, one object per line in each direction. Maximum line length: 2048 bytes excluding newline. Send LF; accept LF or CRLF. JSON strings must escape embedded newlines.

- Gateway → Pi: `hello`, `telemetry`, `command_receipt`, and `gateway_status`.
- Pi → gateway: `event_ack` and `command`.
- `hello` identifies gateway ID, protocol version, firmware version, and radio channel on every USB connection.
- `telemetry` wraps the decoded radio message and source MAC; the Pi checks that mapping against its provisioned nodes.
- No free-form debug prints on this stream. Use a separate debug interface or a structured `gateway_status` message.

Readers accumulate partial reads, handle multiple lines per read, discard an overlong line through its next newline, and resume after malformed input. A partial line is discarded on disconnect. The Pi reconnects automatically; unacknowledged chair events and queued Pi commands recover delivery.

Example gateway-to-Pi telemetry:

```json
{
  "kind": "telemetry",
  "gateway_id": "desk-01",
  "source_mac": "02:00:00:00:00:01",
  "message": {
    "version": 1,
    "type": "OCCUPIED",
    "node_id": "chair-01",
    "boot_id": "ae0172d986e43310",
    "seq": 42,
    "uptime_ms": 310,
    "occupancy": "occupied",
    "battery_mv": 3910,
    "config_revision": 3
  }
}
```

## 10. MQTT contract

Proposed prefix: `smartchair/v1`. Each chair is provisioned to one gateway/Pi bridge.

| Topic suffix                      | Publisher   | QoS / retained | Purpose                                                                                      |
| --------------------------------- | ----------- | -------------- | -------------------------------------------------------------------------------------------- |
| `nodes/{node_id}/events`          | Pi          | 1 / no         | Telemetry events with identity and Pi `received_at`                                          |
| `nodes/{node_id}/state`           | Pi          | 1 / yes        | Latest reconciled occupancy, battery, config revision, observation time, and loss indicators |
| `nodes/{node_id}/commands`        | Application | 1 / no         | Commands with identity and expiry                                                            |
| `nodes/{node_id}/command_results` | Pi          | 1 / no         | Command lifecycle statuses and chair results                                                 |
| `gateways/{gateway_id}/status`    | Pi          | 1 / yes        | Bridge availability and USB connection state                                                 |

Broker ACK does not prove application consumption. Historical replay must not roll retained state backwards. Display staleness separately from occupancy using observation age and heartbeat interval.

Use a retained MQTT last will for bridge offline status; USB-connected status is a separate field. Do not retain commands. Reject commands received with the MQTT retained flag. Restrict broker permissions to the required topic directions and protect credentials in service configuration.

## 11. Acceptance scenarios

Before claiming the protocol works, verify:

1. Bouncing contact produces one stable occupancy transition.
2. Lost radio packets, lost ACKs, and MQTT redelivery produce one logical event after deduplication.
3. USB removal or Pi restart between durable commit and ACK causes safe replay.
4. Broker outage retains acknowledged events on the Pi and publishes them on reconnect.
5. Node reset/deep sleep preserves pending reliable events and does not reuse event identities.
6. An awake, receiving node can receive a queued command; expired commands are not newly dispatched.
7. Ending a session in the Pi application does not alter ESP occupancy, sensing, or sleep behavior.
8. Repeated configuration commands remain safe across reset; invalid configurations change nothing.
9. Malformed, truncated, oversized, unknown-version, and unauthorized-sender packets are rejected without stopping either bridge.
10. Outbox overflow reports lost history and still permits current state recovery.

## 12. Open details and scope

Finalize binary layouts/codes, IMU measurements, USB interface, configuration ranges, recovery ordering, and queue retention rules before implementation. Confirm Pi-storage acknowledgment and acceptable history loss on chair outbox overflow.

Session rules belong to the Pi application. Downstream communication-window scheduling is outside this revision.
