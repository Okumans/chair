# MQTT test console — design

Date: 2026-11-28. Status: approved.

## Intent

A test-only interactive console, the MQTT counterpart of `tools/transport.py`. It acts
as the backend for a **test** bridge: it shows what the chair reports over MQTT and
sends configuration commands through `ChairClient`, so the whole path
(broker → Pi bridge → gateway → chair, and back) can be exercised by hand.

- **Goal:** watch events, state, movement, command progress and bridge status live;
  send a configuration and follow it to its result.
- **Non-goals:** raw radio frames or `hello` (MQTT carries neither; `transport.py`
  covers them at the board); running beside a production backend; contract or bridge
  changes.
- **Anti-goal:** quietly acting as a production backend. Sharing its `consumer_id`
  makes this console's receipts complete deliveries, so a backend could miss events.

## Approaches

- **A. Interactive `ChairClient` console — chosen.** Library only, no bridge changes.
- B. A plus a raw-frame debug topic: rejected; contract, schema and bridge changes
  only for testing, and it weakens the bridge's "valid frames only" rule.
- C. One-shot subcommands (`watch`, `set-config`): rejected as the main form; loses
  interactivity. Can be added later.

## Design

**Entry point:** `tools/mqtt_console.py`, a thin script over `chair_mqtt/console.py`
(same split as `transport.py`). Runs with `uv run`.

**Arguments** (all explicit; it does not read the bridge TOML):

| Argument | Default | Notes |
|---|---|---|
| `--host` | required | |
| `--port` | 8883, or 1883 with `--plaintext` | |
| `--plaintext` | off | Same rule as `BrokerConfig`: loopback hosts only |
| `--ca-file` | system trust store | |
| `--node` | `chair-01` | |
| `--bridge` | required | Must match the test bridge's `bridge_id` |
| `--consumer` | required | Must match the test bridge's `consumer_id` |
| `--db` | new file in a temp directory, deleted on exit | Receipt storage (`SQLiteStorage`) |

Credentials come from `CHAIR_MQTT_USERNAME`/`CHAIR_MQTT_PASSWORD`, like the
service, never from arguments. The MQTT `client_id` is always generated
(`chair-console-<random>`), so it can never take over another client's session. On
start it prints the broker, node, bridge and consumer it is using.

**Output:** one JSON line per item on stdout, tagged with its kind: `event`,
`state`, `movement`, `movement_loss`, `command_update`, `command_error`,
`bridge_status`. Console messages and errors go to stderr, as in `transport.py`.

**Commands:**

- `/config key=value …` — sends a complete configuration. The MQTT contract has no
  configuration readback, so the starting values are the last configuration this
  console sent, or the firmware defaults (900, 3, true, true, 30, 5, 10) if none.
  The revision defaults to the last known `config_revision` + 1. `revision=` overrides
  it. Before sending, it prints the full configuration. Updates print as they arrive
  until the command is applied, rejected or expired. Expiry: 120 s, `expires=`
  overrides it.
- `/state` — the last state received.
- `/status` — the last bridge status received.
- `/help`, `/quit` (Ctrl+D and Ctrl+C also quit).

Input is read with asyncio from stdin, so typing never blocks the MQTT client. One
`/config` at a time; another is refused until the first reaches a terminal status.

## Testing

Unit tests for argument parsing, `key=value` parsing and validation, and the revision
rule. One end-to-end test against a private Mosquitto (skipped without it), using
`ChairBridge` with `FakeGateway`: start the console with piped stdin, check that an
event prints and that `/config` reaches the fake gateway as a `CONFIG_SET`.

## Left to the builder

Exact output field layout, help text wording, and how the receipt temp directory is
cleaned up on abnormal exit.
