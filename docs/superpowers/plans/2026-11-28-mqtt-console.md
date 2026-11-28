# MQTT Test Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An interactive, test-only MQTT console that acts as the backend for a test bridge through `ChairClient`: it prints what the chair reports and sends configuration commands.

**Architecture:** `chair_mqtt/console.py` holds pure helpers (config assignments, revision rule, argument → model conversion), a `Console` class that turns `ChairClient` callbacks into JSON lines and handles typed commands, and `main()`, which runs the client and reads stdin with asyncio. `tools/mqtt_console.py` is a thin launcher, like `tools/transport.py`.

**Tech Stack:** Python 3.13, asyncio, `chair_mqtt` (`ChairClient`, `SQLiteStorage`, `BrokerConfig`, `ClientConfig`, `ChairConfiguration`), pydantic, unittest, Mosquitto for the end-to-end test.

**Spec:** `docs/superpowers/specs/2026-11-28-mqtt-console-design.md`

## Global Constraints

- It never reads the bridge TOML; every identity and broker setting comes from arguments.
- Credentials come only from `CHAIR_MQTT_USERNAME` / `CHAIR_MQTT_PASSWORD`.
- The MQTT `client_id` is always generated: `chair-console-` + 8 random hex characters.
- Firmware defaults: `heartbeat_s=900`, `movement_batch_s=3`, `movement_enabled=true`, `radio_windows_enabled=true`, `radio_interval_s=30`, `radio_open_s=5`, `radio_motion_tail_s=10`.
- Default command expiry 120 s; `set_config` accepts 1..86400.
- Data on stdout as one JSON object per line; messages and errors on stderr.
- Tests run with `uv run --locked python -m unittest discover -s tests`; `make check` must pass. Broker tests use `@unittest.skipUnless(MOSQUITTO, ...)`.

## Review Focus

1. A `/config` value that fails `ChairConfiguration` validation (e.g. `radio_open_s=60` with interval 30): print the error to stderr, send nothing, keep the previous base. Test in Task 1.
2. A second `/config` while one is still pending: refuse with a message, send nothing. Test in Task 3.
3. stdin reaches EOF (piped input): stop the client cleanly, exit 0, remove the temporary database directory. Test in Task 4.
4. `/config` before any state or event is seen: revision becomes 1 and stderr says the revision was guessed. Test in Task 1.
5. Unknown command or malformed `key=value` (no `=`, unknown key, `true`/`false` misspelt): stderr error, the console keeps running. Tests in Tasks 1 and 3.

---

### Task 1: Configuration assignments and the revision rule

**Files:**
- Create: `chair_mqtt/console.py`
- Test: `tests/test_mqtt_console.py`

**Interfaces:**
- Produces:
  - `DEFAULTS: dict[str, int | bool]`, the seven firmware defaults above (no `revision`).
  - `parse_assignments(text: str) -> dict[str, int | bool]`: splits on whitespace; each token is `key=value`. Keys: the seven fields, `revision`, `expires`. Booleans accept exactly `true`/`false`; others must be decimal integers. Raises `ValueError` naming the bad token.
  - `build_configuration(base: dict, known_revision: int | None, assignments: dict) -> tuple[ChairConfiguration, int, bool]`: returns the configuration, expiry seconds (default 120), and whether the revision was guessed. Revision is `assignments["revision"]` if given, else `known_revision + 1`, else `1` (guessed=True). Field values: `base` overlaid with `assignments`. Raises `ValueError` (including pydantic `ValidationError`, which subclasses it) on invalid values.

- [ ] **Step 1: Write failing tests** in `tests/test_mqtt_console.py`, class `AssignmentTests(unittest.TestCase)`:

```python
def test_parses_integers_and_booleans(self):
    self.assertEqual(
        parse_assignments("radio_interval_s=60 movement_enabled=false"),
        {"radio_interval_s": 60, "movement_enabled": False},
    )

def test_rejects_unknown_key_missing_equals_and_bad_boolean(self):
    for text in ("colour=1", "radio_interval_s", "movement_enabled=False", "heartbeat_s=x"):
        with self.subTest(text=text), self.assertRaises(ValueError):
            parse_assignments(text)

def test_revision_follows_the_known_revision(self):
    config, expires, guessed = build_configuration(DEFAULTS, 4, {"radio_interval_s": 60})
    self.assertEqual((config.revision, config.radio_interval_s, expires, guessed), (5, 60, 120, False))
    self.assertEqual(config.heartbeat_s, 900)

def test_revision_is_guessed_without_a_known_revision(self):
    config, _, guessed = build_configuration(DEFAULTS, None, {})
    self.assertEqual((config.revision, guessed), (1, True))

def test_explicit_revision_and_expiry_win(self):
    config, expires, guessed = build_configuration(DEFAULTS, 4, {"revision": 9, "expires": 30})
    self.assertEqual((config.revision, expires, guessed), (9, 30, False))

def test_invalid_window_is_rejected(self):
    with self.assertRaises(ValueError):
        build_configuration(DEFAULTS, 0, {"radio_open_s": 60})
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run --locked python -m unittest discover -s tests -p test_mqtt_console.py`
Expected: ImportError for `chair_mqtt.console`.

- [ ] **Step 3: Implement `DEFAULTS`, `parse_assignments`, `build_configuration`** in `chair_mqtt/console.py`, with a module docstring: "Interactive MQTT test console that acts as the backend for a test bridge."

- [ ] **Step 4: Run tests; expect PASS.** Then `ruff format chair_mqtt/console.py tests/test_mqtt_console.py && ruff check`.

- [ ] **Step 5: Commit** `feat: parse console configuration assignments`

---

### Task 2: Arguments to broker and client settings

**Files:**
- Modify: `chair_mqtt/console.py`
- Test: `tests/test_mqtt_console.py`

**Interfaces:**
- Produces:
  - `build_parser() -> argparse.ArgumentParser`, with `--host` (required), `--port` (int, default None), `--plaintext` (flag), `--ca-file` (Path), `--node` (default `chair-01`), `--bridge` (required), `--consumer` (required), `--db` (Path, default None).
  - `settings_from_args(args, environ) -> tuple[BrokerConfig, ClientConfig]`: port is `args.port`, or 1883 with `--plaintext`, else 8883. `client_id` is `chair-console-` + `secrets.token_hex(4)`. Username and password come from `environ` (empty means None). Pydantic errors propagate.

- [ ] **Step 1: Write failing tests**, class `ArgumentTests`:

```python
def parse(self, *argv):
    return build_parser().parse_args(["--host", "localhost", "--bridge", "pi-01", "--consumer", "backend-01", *argv])

def test_plaintext_defaults_to_1883_and_tls_to_8883(self):
    broker, _ = settings_from_args(self.parse("--plaintext"), {})
    self.assertEqual((broker.port, broker.plaintext), (1883, True))
    broker, _ = settings_from_args(self.parse(), {})
    self.assertEqual((broker.port, broker.plaintext), (8883, False))

def test_client_id_is_generated_per_run(self):
    first, _ = settings_from_args(self.parse(), {})
    second, _ = settings_from_args(self.parse(), {})
    self.assertRegex(first.client_id, r"^chair-console-[0-9a-f]{8}$")
    self.assertNotEqual(first.client_id, second.client_id)

def test_identities_and_credentials(self):
    broker, client = settings_from_args(
        self.parse("--node", "chair-02"),
        {"CHAIR_MQTT_USERNAME": "tester", "CHAIR_MQTT_PASSWORD": "secret"},
    )
    self.assertEqual((client.node_id, client.bridge_id, client.consumer_id), ("chair-02", "pi-01", "backend-01"))
    self.assertEqual((broker.username, broker.password.get_secret_value()), ("tester", "secret"))

def test_remote_plaintext_is_refused(self):
    args = build_parser().parse_args(["--host", "broker.example", "--plaintext", "--bridge", "pi-01", "--consumer", "backend-01"])
    with self.assertRaises(ValueError):
        settings_from_args(args, {})
```

- [ ] **Step 2: Run; expect failures on the missing names.**
- [ ] **Step 3: Implement `build_parser` and `settings_from_args`.**
- [ ] **Step 4: Run tests; expect PASS; format and lint.**
- [ ] **Step 5: Commit** `feat: build console broker and client settings from arguments`

---

### Task 3: `Console`: printing and commands

**Files:**
- Modify: `chair_mqtt/console.py`
- Test: `tests/test_mqtt_console.py`

**Interfaces:**
- Consumes: `DEFAULTS`, `parse_assignments`, `build_configuration` (Task 1).
- Produces: `class Console`:
  - `__init__(self, client, *, out: TextIO, err: TextIO)`: registers handlers on `client` via `on_event`, `on_state`, `on_movement`, `on_movement_loss`, `on_command_update`, `on_command_error`, `on_bridge_status`.
  - Each handler writes `json.dumps({"kind": <kind>, **model.model_dump(mode="json")})` and a newline to `out`, then flushes. Kinds: `event`, `state`, `movement`, `movement_loss`, `command_update`, `command_error`, `bridge_status`. `on_event` handlers take `(event, tx)`; the others take one model.
  - Tracks `last_state`, `last_status`, `base` (starts as a copy of `DEFAULTS`) and `known_revision` (updated from `message.config_revision` of every event, state and movement, and from the revision of each configuration sent).
  - `async handle(self, line: str) -> bool`: returns False for `/quit`, True otherwise. Commands: `/config …`, `/state`, `/status`, `/help`; blank lines are ignored; anything else gives `unknown command; type /help` on `err`.
  - `/config`: parses with `build_configuration`. On `ValueError`, writes `cannot send: <error>` to `err` and returns. If a previous config task is still running, writes `a configuration is still pending; wait for its result` and returns. If the revision was guessed, warns `no revision seen yet; using revision 1`. Writes `sending <json of configuration>` to `err`, calls `await client.set_config(config, expires_in_seconds=expires)`, sets `base` to the sent fields (without revision) and `known_revision` to its revision, then starts a task that `await handle.wait()` and writes `command <id> finished: <status>` to `err`. Updates themselves already print through `on_command_update`.
  - `/state`, `/status`: print the last model in the same JSON-line form on `out`, or `nothing received yet` on `err`.
  - `async close(self)`: cancels a pending wait task.

- [ ] **Step 1: Write failing tests**, class `ConsoleTests(unittest.IsolatedAsyncioTestCase)`, with a `FakeClient` that records handler registrations (`on_*` returns the callback), records `set_config` calls, and returns a handle whose `wait()` awaits an `asyncio.Event` the test controls:

```python
async def test_state_prints_as_a_json_line_and_sets_the_revision(self):
    await self.client.handlers["state"](STATE)  # a SensorState with config_revision=3
    line = json.loads(self.out.getvalue().splitlines()[-1])
    self.assertEqual((line["kind"], line["message"]["config_revision"]), ("state", 3))
    await self.console.handle("/config radio_interval_s=60")
    sent, expires = self.client.sent[-1]
    self.assertEqual((sent.revision, sent.radio_interval_s, expires), (4, 60, 120))

async def test_second_config_is_refused_while_the_first_is_pending(self):
    await self.console.handle("/config")
    await self.console.handle("/config")
    self.assertEqual(len(self.client.sent), 1)
    self.assertIn("still pending", self.err.getvalue())

async def test_next_config_starts_from_the_last_one_sent(self):
    await self.console.handle("/config heartbeat_s=60")
    self.client.finish()  # resolves the pending wait
    await asyncio.sleep(0)
    await self.console.handle("/config radio_interval_s=40")
    sent, _ = self.client.sent[-1]
    self.assertEqual((sent.heartbeat_s, sent.radio_interval_s, sent.revision), (60, 40, 2))

async def test_invalid_config_sends_nothing(self):
    await self.console.handle("/config radio_open_s=60")
    self.assertEqual(self.client.sent, [])
    self.assertIn("cannot send", self.err.getvalue())

async def test_unknown_command_and_quit(self):
    self.assertTrue(await self.console.handle("/bogus"))
    self.assertIn("unknown command", self.err.getvalue())
    self.assertFalse(await self.console.handle("/quit"))

async def test_state_before_any_is_received(self):
    await self.console.handle("/state")
    self.assertIn("nothing received yet", self.err.getvalue())
```

Build `STATE` with `SensorState.model_validate` from a STATE_SYNC payload like the one in `tests/test_mqtt_async_client.py`, with `config_revision=3`, `publication_id=None`, `bridge_id="pi-01"`, `schema_version=1`, `source_mac="02:00:00:00:00:01"`, `received_at_ms` set, and `boot_generation="0000000000000001"`.

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement `Console`.**
- [ ] **Step 4: Run tests; expect PASS; format and lint.**
- [ ] **Step 5: Commit** `feat: print chair MQTT traffic and send configs from the console`

---

### Task 4: `main()`, the launcher, an end-to-end test and docs

**Files:**
- Modify: `chair_mqtt/console.py`
- Create: `tools/mqtt_console.py`
- Test: `tests/test_mqtt_console.py`
- Modify: `docs/TRANSPORT.md` (a short "MQTT test console" subsection after the bench console paragraph), `README.md` (the `tools/` row mentions the MQTT console)

**Interfaces:**
- Consumes: `build_parser`, `settings_from_args` (Task 2) and `Console` (Task 3).
- Produces: `main(argv=None, *, environ=os.environ, stdin=sys.stdin, out=sys.stdout, err=sys.stderr) -> int`, which runs `asyncio.run` of the session:
  1. Parse and build the settings. On a `ValueError`, write it to `err` and return 2.
  2. Use `--db`, or a `tempfile.TemporaryDirectory(prefix="chair-console-")` holding `receipts.sqlite`, removed on exit.
  3. Write `connecting to <host>:<port> as <client_id>; node <n>, bridge <b>, consumer <c>` to `err`, and say that receipts complete deliveries for that consumer.
  4. `async with ChairClient(...)`: build the `Console`, then read stdin lines with `loop.connect_read_pipe` into an `asyncio.StreamReader`. Pass each decoded, stripped line to `console.handle`. Stop on False or EOF. Catch `KeyboardInterrupt` and `asyncio.CancelledError` from Ctrl+C and return 0.
  5. Await `console.close()` before the client stops. Return 0.
- `tools/mqtt_console.py`: shebang and a docstring. It inserts the repository root on `sys.path` like `tools/transport.py` and runs `raise SystemExit(main())`. It has no inline `# /// script` block, since it needs the project dependencies (`uv run tools/mqtt_console.py` from the checkout).

- [ ] **Step 1: Write a failing end-to-end test**, class `ConsoleBrokerTests(unittest.TestCase)`, `@unittest.skipUnless(MOSQUITTO, ...)`. Reuse the broker start pattern from `tests/test_mqtt_async_client.py:start_broker`. Start a `ChairBridge` with `FakeGateway`, `bridge_id="pi-01"`, `consumer_id="backend-01"` and a loopback broker, and wait for `bridge.health().subscriptions_ready`. Run the console as a subprocess:

```python
proc = subprocess.Popen(
    [sys.executable, "tools/mqtt_console.py", "--host", "127.0.0.1", "--port", str(port),
     "--plaintext", "--bridge", "pi-01", "--consumer", "backend-01"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
```

Assertions:
- After `bridge.publish(event(), source_mac=MAC)`, a stdout line with `"kind": "event"` and `message.seq == 1` appears within 10 s. Read stdout on a thread into a queue.
- After writing `/config radio_interval_s=60\n`, `bridge.receive(timeout=10)` returns a notice with `disposition == "queued"`, `request.message.radio_interval_s == 60` and `request.message.revision == 1`.
- After closing stdin, the process exits with code 0 within 10 s (Review Focus 3).

- [ ] **Step 2: Run; expect failure (no launcher).**
- [ ] **Step 3: Implement `main()` and `tools/mqtt_console.py`.**
- [ ] **Step 4: Run the test module; expect PASS.** If the event line doesn't appear, check that `chair.ready` precedes the publish: wait on the console's `connecting` line, then on the first `bridge_status` line before publishing.
- [ ] **Step 5: Write the docs.** In `docs/TRANSPORT.md`, add the command, a note that it is for a **test** bridge (its receipts complete deliveries for `--consumer`), and the command list. Update the README `tools/` row.
- [ ] **Step 6: Run `make check`; expect all tests to pass.**
- [ ] **Step 7: Commit** `feat: add an interactive MQTT test console`
