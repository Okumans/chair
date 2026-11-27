# Smart Chair MQTT specification

Revision 2. This specifies the proposed MQTT interface; the production Pi MQTT
service is not yet implemented.

## Scope and architecture

The Sensor Node measures occupancy and movement and communicates with the Gateway
ESP over the existing ESP-NOW protocol. The gateway forwards ESP messages to the
Pi. The Pi MQTT service exchanges JSON messages with the MQTT broker; backend
applications and controllers use the topics below. ESP nodes do not directly
publish or subscribe to MQTT. This contract targets one Pi, one Gateway ESP, and
one Sensor Node.

The contract preserves ESP message fields, supports complete configuration
commands, and defines reliable event and command-report delivery. Movement is
best effort. MQTT does not change radio scheduling or sensor sleep behavior.
Partial configuration, remote reboot, firmware updates and configuration readback
are outside this contract.

## Versions, identities and encoding

| Item | Rule |
| --- | --- |
| MQTT | MQTT 5.0 over TCP; no silent MQTT 3.1.1 fallback |
| Topic/API version | `smartchair/v1`; outer JSON `schema_version: 1` |
| ESP version | Inner `message.version: 2`; independent of the MQTT version |
| Payload | One UTF-8 JSON object; no JSON-lines framing; empty retained payloads are allowed only for explicit projection tombstones |
| JSON numbers | Integers where specified; booleans are not integers; reject NaN/Infinity and duplicate object keys |
| Payload limit | 16,384 UTF-8 bytes; configuration submissions limited to 4,096 bytes |
| Topic limit | 256 UTF-8 bytes; identifiers contain no `/`, `+`, `#` or NUL |
| `bridge_id`, `consumer_id` | `[A-Za-z0-9_-]{1,32}`; explicitly configured |
| `node_id` | Existing ESP rule: `[A-Za-z0-9_-]{1,15}` |
| Boot IDs and generations | Existing 16-character lowercase hexadecimal strings; never JSON floating-point numbers |
| UUIDs | Canonical lowercase, nonzero UUID strings |
| UTC timestamps | Integer Unix milliseconds in `0..9007199254740991`; `null` only where explicitly allowed |

`bridge_id` is stable across restarts and independent of hostname/PID changes.
An `instance_id` is a new UUID for each service process, used only for diagnostics.

Sensor fact identity is `(node_id, boot_id, seq)`. Backend
deduplication uses this identity, including when the same fact is republished
with another publication ID after an operator-controlled replay.

If the sensor is replaced or its persistent boot generation is reset, configure
the replacement with a new, previously unused `node_id`. Stop dispatching old
commands and clear old retained state and command statuses before using the new
node. This prevents old generation ordering and queued commands from being
mistaken for the replacement's data. A normal reboot or firmware update that
preserves sensor state does not require a new `node_id`.

`publication_id` is a UUID allocated once per durable MQTT publication and stored
with its exact topic/payload. Retries preserve it. Backend ingestion receipts
refer to this ID. MQTT packet identifiers and DUP flags are never database keys.

## Topics

Let `N = smartchair/v1/nodes/<node_id>` and
`B = smartchair/v1/bridges/<bridge_id>`. Names are case-sensitive.
This namespace has no site identifier; if a broker is shared with another
installation, node and bridge IDs must be unique across that broker.
The **Sensor Node** is the ESP that reads the chair sensors; the **Gateway ESP**
forwards its packets to the Raspberry Pi. The **Pi MQTT service** is the planned
service that connects the existing gateway/bridge adapter to the MQTT broker.

| Topic | Information source → intended consumer | MQTT publisher → subscriber | QoS | Retain | Pi storage / backend receipt |
| --- | --- | --- | --- | --- | --- |
| `N/events` | Sensor Node → backend/apps | Pi MQTT service → backend/apps | 1 | No | Durable outbox; backend receipt required |
| `N/movement` | Sensor Node → backend/apps | Pi MQTT service → backend/apps | 0 | No | Bounded RAM only; no receipt |
| `N/movement/loss` | Sensor Node → backend/apps | Pi MQTT service → backend/apps | 0 | No | Bounded RAM only; no receipt |
| `N/state` | Sensor Node → apps; Pi prepares latest snapshot | Pi MQTT service → apps | 1 | Yes | Regenerable latest projection; PUBACK only |
| `N/commands/config` | Controller → Sensor Node via Pi and Gateway ESP | Controller → Pi MQTT service | 1 | **No** | Durable command admission before Pi PUBACK |
| `N/commands/<command_id>/updates` | Pi MQTT service → backend/controllers/apps; includes Sensor Node results when available | Pi MQTT service → backend/controllers/apps | 1 | No | Durable reports; backend receipt required |
| `N/commands/<command_id>/status` | Pi MQTT service → controller/apps; includes Sensor Node results when available | Pi MQTT service → controller/apps | 1 | Yes for the latest projection; no for duplicate responses after cleanup begins | PUBACK only |
| `N/command_errors` | Pi MQTT service → controller/apps | Pi MQTT service → controller/apps | 1 | No | Bounded durable error outbox; PUBACK only |
| `B/receipts` | Designated backend → Pi MQTT service | Designated backend → Pi MQTT service | 1 | **No** | Commit receipt before Pi PUBACK |
| `B/status` | Pi MQTT service or broker Will → apps | Pi MQTT service or broker Will → apps | 1 | Yes | Live bridge status; no backend receipt |

Sensor-originated information travels Sensor Node → Gateway ESP → Pi MQTT service
→ MQTT broker → backend/apps. Configuration commands travel in the reverse
direction, with the Pi admitting and dispatching them before the Sensor Node can
apply them. MQTT command reports are generated by the Pi; queued status and
admission errors do not originate on the Sensor Node.

Do not retain commands, raw events, movement, receipts or report-update streams.
Do not subscribe the bridge to its own upstream event topics. It subscribes only
to its configured node's exact command topic and its own receipt topic.

All JSON publications set MQTT Payload Format Indicator to UTF-8 and Content Type
to `application/json`. Reliable events/reports have no MQTT Message Expiry.
Controllers set Message Expiry on commands to
`ceil((expires_at_ms - controller_now_ms) / 1000)`, recomputed on each retry.
The unchanged payload deadline remains authoritative at the Pi.

Command/receipt subscriptions use QoS 1, Retain Handling 2 and Retain As Published
true. The Pi rejects any received command/receipt with RETAIN set, including a
live retained publication. These subscription options avoid treating a stored
retained value as a new instruction; see
[MQTT 5, section 3.8.3.1](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html).

## ESP → MQTT mapping

After validating the source and committing reliable data, publish this
on `smartchair/v1/nodes/chair-01/events`:

```json
{
  "schema_version": 1,
  "bridge_id": "pi-01",
  "publication_id": "d5307c2e-1bbc-4cea-bd6d-87065ab17a0f",
  "source_mac": "02:00:00:00:00:01",
  "received_at_ms": 1800000000123,
  "boot_generation": "0000000000000007",
  "message": {
    "version": 2,
    "type": "STATE_SYNC",
    "node_id": "chair-01",
    "boot_id": "41c966a5ce723b09",
    "seq": 120,
    "uptime_ms": 4300,
    "occupied": true,
    "battery_mv": null,
    "config_revision": 2,
    "health_flags": 2,
    "boot_generation": "0000000000000007"
  }
}
```

The `message` object is the ESP JSON packet, unchanged in meaning and field types.
Only field order/whitespace may differ. Do not rename `occupied`, scale raw axes,
convert identities to decimal, invent timestamps on the sensor, replace null
battery values with zero, or add MQTT metadata inside that object.

`source_mac` comes from the validated USB envelope, not a cloud-supplied value.
`received_at_ms` is the Pi's **first** receipt time; it is not an acquisition UTC
time. It may be null when UTC is untrusted. `boot_generation` outside `message`
comes from the Pi's established boot map; it is null if the boot is not yet known.
Do not add a generation to inner packets whose wire type does not carry it.
Freeze this metadata in the publication record; a retry must not become a new
observation. Never derive event ordering from uptime or MQTT arrival order.

### Routing every existing packet type

| ESP type | MQTT treatment |
| --- | --- |
| OCCUPIED, VACATED, HEARTBEAT, STATE_SYNC, BATTERY_LOW | `N/events`, reliable envelope |
| COMMAND_RESULT | `N/events`, plus correlated command-report update when the command is known |
| MOVEMENT_BATCH | `N/movement`, same envelope but `publication_id: null`; QoS 0 |
| MOVEMENT_LOSS | `N/movement/loss`, `publication_id: null`; QoS 0 |
| COMMAND_RECEIPT | Local command progress input; only `received` advances progress; no raw public event |
| READY, READY_PROOF | Local transport controls; no MQTT projection or replay |
| EVENT_ACK, READY_PROBE | Generated locally by the existing protocol adapter; not cloud APIs |
| CONFIG_SET | Accepted only through the command-template contract below |

The [ESP protocol specification](PROTOCOL_SPEC.md) defines frame ranges and exact fields,
signed axes, strictly increasing sample offsets, maximum 75 samples and valid
empty final batches. JSON represents samples as `offset_ms, ax, ay, az, gx, gy,
gz`; `samples.length` is the wire count. Accel/gyro range bytes are currently fixed
by the codec and are not extra JSON fields. Preserve `end_reason` and all actual
offsets. MQTT service drops must not be added to the sensor's `dropped_batches`;
count them separately as `mqtt_best_effort_dropped` in bridge diagnostics.

### Movement batch example

A complete `MOVEMENT_BATCH` published to
`smartchair/v1/nodes/chair-01/movement`:

```json
{
  "schema_version": 1,
  "bridge_id": "pi-01",
  "publication_id": null,
  "source_mac": "02:00:00:00:00:01",
  "received_at_ms": 1800000000123,
  "boot_generation": "0000000000000007",
  "message": {
    "version": 2,
    "type": "MOVEMENT_BATCH",
    "node_id": "chair-01",
    "boot_id": "41c966a5ce723b09",
    "seq": 120,
    "uptime_ms": 4300,
    "occupied": true,
    "battery_mv": null,
    "config_revision": 2,
    "health_flags": 2,
    "batch_start_ms": 4000,
    "duration_ms": 40,
    "end_reason": "stopped",
    "samples": [
      {
        "offset_ms": 0,
        "ax": -32768,
        "ay": 32767,
        "az": 16384,
        "gx": 0,
        "gy": -1,
        "gz": 2
      },
      {
        "offset_ms": 40,
        "ax": 1,
        "ay": 2,
        "az": 3,
        "gx": 4,
        "gy": 5,
        "gz": 6
      }
    ]
  }
}
```

Each axis is a signed 16-bit raw sensor count; `offset_ms` is an unsigned
16-bit offset from `batch_start_ms`. Acceleration uses ±2 g (16,384 counts/g)
and angular velocity uses ±250 degrees/s (131 counts per degree/s). The sensor
samples at approximately 25 Hz while occupied; batches contain movement and
debounce samples, not a continuous stream of idle readings. Empty batches are
allowed only for final `stopped`, `vacant`, or `disabled` boundaries.

A `MOVEMENT_LOSS` message uses the same envelope with a `message` containing
`type: "MOVEMENT_LOSS"` and a nonzero uint32 `dropped_batches`. It is itself
best effort.

### Retained state

`N/state` contains the same envelope shape with `publication_id: null` and the
latest generation/sequence-qualified **reliable** telemetry packet in `message`.
It represents the last durable physical snapshot, not current RF availability,
online status or a complete sensor configuration.

The retained projection must not include best-effort packets or fabricated
STATE_SYNC packets. READY, movement and receipts must not trigger retained-state
updates. Establish a boot's generation through a genuine STATE_SYNC, then choose
the greatest `(generation, seq)` among eligible persisted reliable facts. When a
previously unknown boot is established, reconsider its already stored facts.
Old generations/sequences never replace newer state. A sensor replacement or
state reset uses a new node ID and clears the old retained state as described above.

Only one publication per retained topic may be in flight. Coalesce unsent
projections, never an in-flight payload, and publish their latest value after
the earlier one finishes. After reconnect/restart, retry the outstanding older
projection before its newer successor. This prevents a late retry from replacing
fresh retained state with an older snapshot.

## MQTT → ESP configuration command

Publish to `smartchair/v1/nodes/chair-01/commands/config`:

```json
{
  "schema_version": 1,
  "expires_at_ms": 1800000300000,
  "message": {
    "version": 2,
    "type": "CONFIG_SET",
    "node_id": "chair-01",
    "command_id": "37c7984c-33eb-4dcc-93be-e2b11bb2436d",
    "revision": 3,
    "heartbeat_s": 900,
    "movement_batch_s": 3,
    "movement_enabled": true,
    "radio_windows_enabled": true,
    "radio_interval_s": 30,
    "radio_open_s": 5,
    "radio_motion_tail_s": 10
  }
}
```

This is an ESP-shaped **command template**, not a complete wire frame. The
controller supplies the command UUID and complete configuration. It must omit
`boot_id` and `seq`; the Pi allocates its own control identity on each dispatch
attempt. Those header values are not the sensor's boot/sequence and are not the
command's idempotency key. Unknown fields, including caller-supplied headers,
partial patches, arbitrary ACK targets and `source_mac`, are rejected.

The bridge converts the template into the existing gateway command format. MQTT
metadata does not enter the ESP frame.

Dispatch accepted jobs FIFO, with at most one active command for the node. A later
request does not silently replace an older one. Busy/no response keeps the head
eligible until its result or expiry; terminal/expired work releases the next job.

### Admission and idempotency

1. Require the exact allowed topic, QoS 1 delivery, no RETAIN, valid bounded JSON,
   supported versions and a matching topic/body node.
2. Validate the complete configuration against the ESP protocol.
   Revision is uint32 including zero; intervals/tail are nonzero uint32; booleans
   are exact; open time must not exceed interval. Do not silently increase revision.
3. Look up the command UUID **before** rejecting an already known request because
   its deadline has passed. A semantic duplicate returns its current status and
   never reinserts it or creates another radio job.
4. Canonical identity content is `(node, expires_at_ms, command
   template)`. Normalize JSON key order/whitespace only. Same UUID with different
   content or deadline is `command_id_conflict`; preserve the original command
   and its retained status. MQTT properties and delivery IDs are not hash inputs.
5. Require trusted Pi UTC. A new request whose expiry has already passed is
   stored as `expired` without a radio job. Reject a new request whose expiry is
   more than 24 hours ahead.
6. In one transaction persist the request/fingerprint, local command job, initial
   status report and both its MQTT update/projection work. Then acknowledge the
   inbound MQTT delivery. Controller-to-broker PUBACK alone is not Pi admission;
   the controller waits for a `queued`, `expired` or terminal command report.

Identical MQTT redelivery after a crash must repair/retry existing report work,
not execute a new command. Preserve UUID history/fingerprints through ordinary
restarts and do not silently drop a command tombstone to permit UUID reuse.

If the retained status has been selected for deletion or already removed, each
valid duplicate request schedules a non-retained QoS 1 `COMMAND_STATUS` response
on that command's `/status` topic. It contains the current saved report with
`publication_id: null` and its existing `report_seq`; it does not recreate a
retained value, restart the retention period, or execute the command again. This
also applies to a known duplicate received after its original deadline. Persist
the response work before acknowledging the duplicate request, then remove it
after successful broker PUBACK. Pending responses for the same command may be
coalesced to the newest report, but a duplicate received after an earlier response
was completed must schedule another response. If response work cannot be saved,
withhold the incoming acknowledgement and recover instead of silently dropping it.
The response is PUBACK-only and does not require a backend receipt; it does not
replace or alter reliable `/updates` work. A broker PUBACK does not establish
that a particular controller received the response.

Admission errors use `N/command_errors`, with `schema_version`, `bridge_id`,
`error_id` UUID, safely parsed `command_id` or null, `reason`,
`retryable` boolean and `received_at_ms`. Reasons include `invalid_json`,
`invalid_schema`, `unsupported_version`, `unknown_node`, `node_mismatch`,
`retained_command`, `invalid_config`, `clock_untrusted`,
`deadline_too_far`, `command_id_conflict` and `queue_full`. Do not echo untrusted
request bodies or broker credentials. A durable rejection is followed by MQTT ACK
so a poison message does not loop. If its bounded error journal cannot commit,
withhold ACK and disconnect/back off. Error reports never overwrite a valid
command's canonical status. A retryable admission error has not queued a job;
the caller may retry the same unchanged request while its expiry is still valid.

An admission error on `N/command_errors` has this form:

```json
{
  "schema_version": 1,
  "bridge_id": "pi-01",
  "error_id": "a270cc9d-b7a6-46d8-983d-14daf1447a56",
  "command_id": null,
  "reason": "invalid_json",
  "retryable": false,
  "received_at_ms": null
}
```

### Required controller retry behavior

The controller must keep a durable submission outbox containing the exact topic,
request bytes/semantic content, command UUID and original expiry before publishing.
Subscribe to that UUID's `/status` and `/updates` topics at QoS 1 and wait for
successful SUBACKs granting QoS 1 before the first submission. Re-establish the
subscriptions, or confirm that a live session resumed them, before retrying after
reconnection. Process both retained status projections and non-retained status
responses; do not require the received RETAIN flag to distinguish them. Until a
matching command report confirms Pi admission (`queued`, `delivered` or a terminal
status), retry the same request every 15 seconds, including after controller restart
or broker reconnect. Recompute only MQTT Message Expiry; never extend the payload
deadline or allocate a new UUID to retry the same operation. A broker PUBACK alone
must not clear this controller outbox.

Process reports from both topics by `(node_id, command_id, report_seq)`, keeping
the greatest accepted `report_seq` durably. Ignore duplicate or older reports
instead of repeating actions or rolling back state. A status projection or
response uses `publication_id: null`, whereas the equivalent `/updates` report
has its own publication ID; compare report content excluding this publication
metadata when checking the same sequence for conflicting content. Diagnose a
same-sequence content conflict rather than silently accepting it.

A matching admission report ends submission retries, not monitoring of the command.
At the request deadline stop submitting. If no admission report was ever observed,
record a controller-local `admission_unknown` outcome; do not assert that the Pi
rejected the request or that the sensor did not execute it. Keep monitoring for an
authentic late report on `/updates`, including after the retained `/status` has
been removed. Controllers that need late results must keep the `/updates`
subscription active while monitoring. Updates are non-retained: a controller
that is offline may miss one if its broker session expires or is reset; use a
backend history or replay facility if recovery across such gaps is required. This
specification does not define a controller history-query API. The retry requirement
is essential because a fresh Pi process resets its broker session as specified below.

For example, if a terminal status is evicted before a controller receives it, the
controller's next identical request causes a fresh non-retained `/status` response.
If the controller has already received `expired` and a sensor result arrives after
cleanup, the later `/updates` report has a higher `report_seq` and carries the late
result without recreating the retained status.

### Deadline semantics

Expiry is the latest time the Pi may initiate/continue submission, not a promise
that the sensor will never apply an already transmitted command afterward. The
sensor has no UTC deadline field. Bytes already accepted by serial/radio hardware
cannot be recalled. There is no cancellation or automatic rollback.

Use authoritative integer-millisecond deadlines and recheck before dispatch.
Also cap an admitted job with a monotonic deadline derived from remaining lifetime
so a backward UTC adjustment does not extend it within the same process. On a
restart recalculate only after UTC is trusted. Forward clock jumps may expire work
early. Loss of clock trust pauses new admission and dispatch, while reliable sensor
storage and forwarding continue; unknown reception UTC is represented as null.

Do not dispatch again after expiry. An authentic late result is still persisted,
forwarded and ACKed, and updates the report's result evidence while its `expired`
status remains expired. Never equate expired/no response with proof of non-execution.

## Command reports and sensor results

The command lifecycle uses these statuses:

| Status | Trigger / meaning |
| --- | --- |
| `queued` | Pi durably accepted the command; execution is pending |
| `delivered` | Sensor COMMAND_RECEIPT says `received`; not proof of application |
| `applied` | Reliable COMMAND_RESULT says `applied` or `already_applied` |
| `rejected` | Reliable COMMAND_RESULT reports another terminal outcome |
| `expired` | Queued/delivered request passed its dispatch deadline |

`queued → applied/rejected` is valid when the receipt was lost. `queued` or
`delivered` may become `expired`. Applied/rejected/expired are terminal for
dispatch. `COMMAND_RECEIPT busy` leaves the job eligible for the existing retry
policy; it does not finalize it. A reliable `COMMAND_RESULT busy`, if received,
maps to `rejected` consistently with the present store, retaining the raw status.
Other receipt statuses do not fabricate a terminal outcome.

The first terminal result finalizes applied/rejected status. Later result facts
remain archived; an inconsistent later result must produce a diagnostic rather
than silently reverse the command's established outcome. Expired commands retain
their first late-result evidence and record subsequent facts without rescheduling.

Every meaningful report change increments persisted `report_seq` in
`1..9007199254740991`. Exhaustion fails closed. Publish each change on `/updates`
with a stable publication ID; publish the latest retained `/status` with
`publication_id: null` while that status remains eligible under the retention
policy below. The retained projection follows the per-topic ordering rule in the
retained-state section. Clients order reports by node ID, command UUID and
report_seq, not MQTT arrival order. Republishing the same status after duplicate
admission does not increment report_seq.

An applied report looks like:

```json
{
  "schema_version": 1,
  "type": "COMMAND_STATUS",
  "bridge_id": "pi-01",
  "publication_id": "cae4ebbd-b782-42b2-bff6-c571360d253c",
  "command_id": "37c7984c-33eb-4dcc-93be-e2b11bb2436d",
  "report_seq": 2,
  "status": "applied",
  "requested_revision": 3,
  "expires_at_ms": 1800000300000,
  "updated_at_ms": 1800000001500,
  "late_result": false,
  "reason": null,
  "message": {
    "version": 2,
    "type": "COMMAND_RESULT",
    "node_id": "chair-01",
    "boot_id": "41c966a5ce723b09",
    "seq": 121,
    "uptime_ms": 5677,
    "occupied": true,
    "battery_mv": null,
    "config_revision": 3,
    "health_flags": 2,
    "command_id": "37c7984c-33eb-4dcc-93be-e2b11bb2436d",
    "status": "applied"
  }
}
```

Before a result, `message` is null. `updated_at_ms` may be null if clock trust is
lost after admission. `requested_revision` comes from the command; the result's
`config_revision` is the sensor's active revision at result creation. A repeated
UUID can return a historical outcome without rolling back newer settings; do not
treat that outcome as full current-configuration readback. Actual result JSON
does not gain a new `revision` field.

An unknown command's authentic COMMAND_RESULT is still a reliable sensor fact
and must be stored/forwarded/ACKed. Record `unmatched_command_result` locally;
do not invent an accepted request, deadline or canonical command-status row.

### Retained command-status cleanup

The Pi MQTT service manages cleanup as part of its own work; no separate scheduled
job is required. The following defaults apply to `N/commands/<command_id>/status`:

- Retain statuses for active (`queued` or `delivered`) commands until they become
  terminal. Active statuses are not evicted to make room for completed statuses.
- After the first transition to `applied`, `rejected`, or `expired`, retain the
  terminal status for at most **7 days**, subject to the capacity limit below.
  Duplicate commands, retries and late results do not restart this period.
- Keep at most **256 retained terminal statuses**. If a new terminal status would
  exceed that limit, remove the oldest retained terminal status first. Eviction
  can therefore remove a status sooner than 7 days. The cap applies to the
  broker's retained terminal statuses, not just pending projection work.

Persist the first terminal time, the retention deadline and cleanup work with the
terminal report. Check for due cleanup on service startup and periodically while
running. If the terminal time was recorded while UTC was untrusted, start the
7-day period from the first later trusted observation; do not guess an earlier
deadline. When UTC is untrusted, do not perform time-based expiry. The capacity
limit still applies; use persisted terminal-transition order to select the oldest
eligible status, and stop publishing new terminal projections if no slot is safe.

For expiry or capacity eviction, the service queues a **zero-byte retained QoS 1
publication** on the exact status topic. Persist this deletion until a successful
broker PUBACK and retry it after restart or reconnection. Count the old retained
status against the limit until that acknowledgement; do not publish the replacement
terminal projection while doing so would exceed the cap. This may temporarily leave
an older status visible while cleanup is pending. If the broker is unavailable,
cleanup can occur later than 7 days and no immediate broker-side deletion is
guaranteed. A successful deletion removes the broker's retained value; it does not
delete messages already received by subscribers.

Serialize status publications and deletion for each topic. Before sending a
deletion, settle or fence older in-flight publications and discard any queued
older projection so it cannot reappear after deletion, including after reconnect
or restart. Once a command is selected for deletion, retries, duplicate commands,
and late sensor results must not recreate its retained status. A duplicate request
instead receives the non-retained `/status` response defined above. Continue to
publish authentic late results through the reliable `/updates` stream, even after
status cleanup. Cleanup must not delete unacknowledged reliable updates or alter
their backend-receipt requirements.

After cleanup, a controller subscribing late cannot obtain a retained status
from the broker. It may retry the same request before its deadline to solicit a
non-retained response while subscribed; history is available only if a backend
provides it. This policy
does not remove command history or deduplication records on the Pi or backend,
and therefore does not by itself bound their database storage or allow command ID
reuse. Those records require a separate retention policy.

## Reliable delivery and backend receipts

Outbox records store the exact publication ID, topic, UTF-8 payload, delivery class,
expected consumer, logical fact/report key, first creation time, retry state and
broker/backend acknowledgement state. Sensor fact and command-report payloads
are immutable after commit. New receipt metadata does not rewrite their payload.

For receipt-required rows:

1. Publish QoS 1. A successful `publish()` enqueue is not broker delivery.
2. Track broker PUBACK separately. Start a 30-second monotonic acknowledgement
   deadline at successful submission to the bounded client queue. A negative
   reason code is classified below; it is never a successful delivery. Do not
   free durable work on a disconnect.
3. Wait for the designated backend's receipt. If absent after 15 seconds, republish
   with the same publication ID, exponentially backing off to 300 seconds with
   jitter. Never start an application retry while that row is still MQTT-in-flight.
4. Commit an accepted receipt, then remove the pending payload or mark it completed.
   Keep the original event/report archive and a durable completion tombstone keyed
   by both publication ID and logical fact/report key. A later sensor duplicate
   must recognize that the publication already completed rather than enqueue it
   again. Pending quotas count only unfinished work; deleting a completed payload
   does not delete this deduplication evidence.

### Publication state and stalled broker recovery

Persist logical states `PENDING`, `MQTT_INFLIGHT`, `WAITING_RECEIPT`, `COMPLETED`
and `QUARANTINED`, keeping broker-attempt history separate from the immutable
publication. A successful native PUBACK moves receipt-required work to
WAITING_RECEIPT and PUBACK-only work to COMPLETED. An application receipt may
complete any unfinished receipt-required row, including a quarantined one, if
its publication/expected-consumer identity matches.

A 30-second broker-ACK timeout applies even while PINGRESPs arrive, and even if a
backend receipt already completed the logical row. That native transaction still
occupies an MQTT in-flight slot until its PUBACK or connection recovery. On timeout
close/abort the connection, invalidate its incoming ACK tokens, and reconnect with
backoff; preserve the durable publication and any live client's protocol state.
Do not timer-resend an unacknowledged native PUBLISH on an open connection. Same-
connection application retries are *new* MQTT publications only after the earlier
native attempt has completed and the backend receipt is overdue. This follows
[MQTT 5 section 4.4](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html).

Monitor SUBACK with the same 30-second deadline. Until all required subscriptions
are positively acknowledged with the required granted QoS, expose
`subscriptions_ready: false`, keep incoming commands fenced from execution, and
recover the connection on timeout. This
handshake gate applies to newly ingested MQTT requests; already admitted local
jobs continue under their existing deadline/transport policy during MQTT outages.
Connection health must not be inferred solely from TCP or successful PINGRESP.

### Negative acknowledgements and quarantine

| Broker response | Action |
| --- | --- |
| PUBACK `0x00` or `0x10` | Broker accepted; `0x10` does not mean a backend received it; continue waiting for MQTT_ACK when required |
| `0x97` quota exceeded | Retain work, release native attempt, retry with 1..300 s exponential backoff/jitter; alert if persistent |
| `0x87` not authorized, `0x90` topic invalid, `0x99` payload format invalid | Persist QUARANTINED with reason, attempt and diagnosis; no automatic application retry |
| `0x91` packet identifier in use | Stop publishing on that connection and recover session/client state; quarantine an unresolved repeated mismatch |
| Other failure/unknown reason | Fail closed into QUARANTINED and diagnose; do not guess that it is transient |
| DISCONNECT packet too large / local oversized payload | Preserve and quarantine offending work; do not retry unchanged bytes forever |

Quarantine never deletes the fact, grants a backend receipt, marks a command
applied, or frees its pending-payload accounting. It releases the native attempt
so one poison publication does not block unrelated deliverable work. Expose
quarantined count/oldest age and a reason histogram. After fixing broker policy
or a validated serialization issue, an explicit local administrative redrive may
retry the *same* publication ID and bytes. Changed topic/payload requires an
explicit replacement publication with a new ID linked to the original; retain
its audit history. Never edit already-published bytes under an existing ID.

The backend must subscribe to `/events` and `/commands/+/updates`, persist each
publication and its semantic identity/report_seq before acknowledging ingestion,
and send this on the publishing bridge's `B/receipts` topic. A repeated
publication ID with a different topic or payload hash is a contract violation:
reject/diagnose it without issuing an ingestion receipt. An equal semantic sensor
fact under a different publication ID is stored as another delivery of the same
fact, not another occupancy transition. The receipt payload is:

```json
{
  "schema_version": 1,
  "type": "MQTT_ACK",
  "consumer_id": "backend-01",
  "publication_id": "d5307c2e-1bbc-4cea-bd6d-87065ab17a0f"
}
```

This is a Pi/backend control object, **not** an ESP EVENT_ACK. The Pi must never
translate it into a radio ACK. It can complete only an existing publication whose
expected consumer matches. A broker ACL must bind this receipt topic to the
designated backend account; the subscriber cannot infer a publisher's authenticated
identity from MQTT payload fields alone. Validate the exact receipt field set,
version/type, canonical UUID, configured consumer, QoS and non-retained delivery.
Invalid receipts are durably diagnosed and MQTT-ACKed without changing outbox or
radio state; they are not executed as ESP controls. The diagnosis journal keeps the
most recent entries and a durable total; when full it evicts its oldest entry rather
than withholding the ACK. Only a failed commit withholds the ACK.

The backend re-sends a receipt for duplicate publications, including after a crash
between its database commit and receipt publication. It must not repeat business
side effects when recognizing an already ingested event. Backend receipt ingestion
on the bridge is acknowledged only after durable commit. Duplicate/unknown completed
receipt IDs are ignored and MQTT-ACKed; they never recreate work. An authenticated
receipt can arrive before the sender's PUBACK callback; completing that row is
valid and a later broker callback is a no-op.

This gives at-least-once publication until **durable backend ingestion**, with
identity-based deduplication, under eventual connectivity, sufficient storage and
a conforming backend. It does not promise exactly-once business effects, survival
of simultaneous loss of all durable copies, or complete history through the ESP's
existing bounded outbox overflow. Additional dashboards need not send receipts.

## MQTT session and restart behavior

Use a stable client ID `smartchair-<bridge_id>`, MQTT 5, Session Expiry
86,400 seconds, Keep Alive 30 seconds and Receive Maximum 16. Readiness requires
successful CONNACK and all required SUBACKs at the requested QoS. A QoS 1
subscription granted at QoS 0 must fail closed.

A fresh client process starts a clean broker session and resubscribes; a live
client reconnecting with intact protocol state may resume its broker session.
If the session is absent, resubscribe and recover durable application work.
Never acknowledge a delivery token from an earlier connection as if it belonged
to the current connection. Persisted application work is not a substitute for
client-side MQTT protocol session state. Resetting a session may discard commands
not yet admitted; controller retries recover them before their original expiry.

Reconnect with exponential delay of 1–30 seconds plus jitter. Broker outages must
not stop sensor acquisition, sensor acknowledgements, or already admitted local
command work.

## Bridge presence, freshness and retained data

Before CONNECT, install a retained QoS 1 Will on `B/status`, with
`schema_version: 1`, `type: "BRIDGE_STATUS"`, `bridge_id`, `instance_id`,
`status: "offline"`, `reason: "connection_lost"` and `observed_at_ms: null`.
Use Will Delay 0. Do not put a fabricated disconnect timestamp in a prebuilt Will.

After successful connection, publish `status: "online"` with current observation
time and current `gateway_connected`, `gateway_ready`, `storage_ok`, pending
publication count, overdue backend-receipt count and best-effort drop count.
The exact online snapshot keys are `gateway_connected`, `gateway_ready`,
`storage_ok`, `subscriptions_ready`, `pending_publications`, `overdue_receipts`,
`quarantined_publications` and `mqtt_best_effort_dropped`. Readiness is false until
required subscriptions are acknowledged. Quarantine diagnostics also expose an
operator-readable oldest age/reason histogram outside the bounded status payload.
Refresh on material changes and at most once per ten seconds for counters.
Unknown values are null; a broker connection loss does not prove USB disconnected.

On graceful shutdown publish offline status and wait up to two seconds for PUBACK
outside the serial loop. Send a normal DISCONNECT only after confirmation;
otherwise abort the connection so the broker can publish the Will. Will-based
failure detection is not instantaneous.

Online means **the Pi MQTT service is connected**, not that a sensor is occupied,
awake, listening or recently observed. `N/state` is a last-known fact with a first
receipt time. Do not synthesize vacancy/offline state after an arbitrary timeout.
When no qualified state exists, clear an obsolete retained state rather than
publish a fabricated STATE_SYNC. Reconnect republishes current projections; it
does not replay transport-control messages as fresh availability.


Example online bridge status:

```json
{
  "schema_version": 1,
  "type": "BRIDGE_STATUS",
  "bridge_id": "pi-01",
  "instance_id": "5fd84ecd-e28f-48fb-9b9b-f7041f040461",
  "status": "online",
  "reason": null,
  "observed_at_ms": 1800000000123,
  "gateway_connected": true,
  "gateway_ready": true,
  "storage_ok": true,
  "subscriptions_ready": true,
  "pending_publications": 0,
  "overdue_receipts": 0,
  "quarantined_publications": 0,
  "mqtt_best_effort_dropped": 0
}
```

## Bounds and backpressure

The following bounds apply to the bridge by default:

| Resource | Limit / behavior |
| --- | --- |
| Accepted nonterminal commands | 64; reject new admission with `queue_full` |
| Receipt-required pending outbox | 10,000 rows or 64 MiB of payload, whichever comes first |
| In-flight MQTT publications | 16 total, subject to broker limits |
| Best-effort MQTT queue | 128 messages; drop oldest and increment the bridge-specific counter |
| Retained terminal command statuses | 256, for at most 7 days when cleanup is available; see retained command-status cleanup |
| Pending command error reports | 1,024 records and 2 MiB; each error no more than 2,048 bytes |
| Pending retained projections | 1,024 topics and 32 MiB of desired/in-flight payload |
| Invalid-delivery diagnoses | 1,024 most recent entries per journal (bridge receipts, backend deliveries) plus a durable total; evict the oldest |
| Free-disk safety threshold | 64 MiB; stop new durable admissions below it |

Do not acknowledge new reliable sensor facts until their required forwarding work
is durably recorded. Under pressure, continue acknowledging committed duplicates,
processing receipts, expiring existing commands and draining pending work. Never
silently downgrade or delete pending reliable work. Best-effort movement can be
dropped; MQTT-side drops must be counted separately from Sensor Node loss.
Reserve capacity for accepted commands' required reports and results, so accepted
work cannot be stranded merely because new work fills the outbox. Each accepted
nonterminal command reserves three future publication slots of 16,384 bytes each
in addition to its initial report. Release unused reservations only after its
terminal report is durably recorded. Finite sensor
and bridge storage mean prolonged outages can still lose detailed event history.

## Security

- Use authenticated MQTT accounts and CA/hostname-verified TLS for network
  connections; plaintext is for explicitly configured local development only.
- Restrict each bridge to its assigned node output, command and receipt topics.
  Controllers have access to authorized nodes; only the designated backend may
  publish receipts. Dashboard accounts are read-only.
- Reject invalid topics, mismatched identities, unsupported versions, retained
  commands and oversized or malformed payloads before dispatch.
- Never place broker credentials or ESP pairing keys in MQTT payloads or logs.
