import asyncio
import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gateway_fake import FakeGateway
from test_mqtt_async_client import MOSQUITTO, start_broker

from chair_mqtt import BridgeConfig, BrokerConfig, ChairBridge
from chair_mqtt.client import SensorEvent, SensorState
from chair_mqtt.console import (
    DEFAULTS,
    Console,
    build_configuration,
    build_parser,
    main,
    parse_assignments,
    settings_from_args,
)


class AssignmentTests(unittest.TestCase):
    def test_parses_integers_and_booleans(self):
        self.assertEqual(
            parse_assignments("radio_interval_s=60 movement_enabled=false"),
            {"radio_interval_s": 60, "movement_enabled": False},
        )

    def test_rejects_unknown_key_missing_equals_and_bad_boolean(self):
        for text in (
            "colour=1",
            "radio_interval_s",
            "movement_enabled=False",
            "heartbeat_s=x",
            "movement_enabled=1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_assignments(text)

    def test_revision_follows_the_known_revision(self):
        config, expires, guessed = build_configuration(
            DEFAULTS, 4, {"radio_interval_s": 60}
        )
        self.assertEqual(
            (config.revision, config.radio_interval_s, expires, guessed),
            (5, 60, 120, False),
        )
        self.assertEqual(config.heartbeat_s, 900)

    def test_revision_is_guessed_without_a_known_revision(self):
        config, _, guessed = build_configuration(DEFAULTS, None, {})
        self.assertEqual((config.revision, guessed), (1, True))

    def test_explicit_revision_and_expiry_win(self):
        config, expires, guessed = build_configuration(
            DEFAULTS, 4, {"revision": 9, "expires": 30}
        )
        self.assertEqual((config.revision, expires, guessed), (9, 30, False))

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError):
            build_configuration(DEFAULTS, 0, {"radio_open_s": 60})


class ArgumentTests(unittest.TestCase):
    def parse(self, *argv, host="localhost"):
        return build_parser().parse_args(
            ["--host", host, "--bridge", "pi-01", "--consumer", "backend-01", *argv]
        )

    def test_plaintext_defaults_to_1883_and_tls_to_8883(self):
        broker, _ = settings_from_args(self.parse("--plaintext"), {})
        self.assertEqual((broker.port, broker.plaintext), (1883, True))
        broker, _ = settings_from_args(self.parse(), {})
        self.assertEqual((broker.port, broker.plaintext), (8883, False))

    def test_explicit_port_wins(self):
        broker, _ = settings_from_args(self.parse("--plaintext", "--port", "1999"), {})
        self.assertEqual(broker.port, 1999)

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
        self.assertEqual(
            (client.node_id, client.bridge_id, client.consumer_id),
            ("chair-02", "pi-01", "backend-01"),
        )
        self.assertEqual(
            (broker.username, broker.password.get_secret_value()),
            ("tester", "secret"),
        )

    def test_empty_credentials_mean_none(self):
        broker, _ = settings_from_args(
            self.parse(), {"CHAIR_MQTT_USERNAME": "", "CHAIR_MQTT_PASSWORD": ""}
        )
        self.assertEqual((broker.username, broker.password), (None, None))

    def test_remote_plaintext_is_refused(self):
        with self.assertRaises(ValueError):
            settings_from_args(self.parse("--plaintext", host="broker.example"), {})


STATE = SensorState.model_validate(
    dict(
        schema_version=1,
        bridge_id="pi-01",
        publication_id=None,
        source_mac="02:00:00:00:00:01",
        received_at_ms=1800000000000,
        boot_generation="0000000000000001",
        message=dict(
            version=2,
            type="STATE_SYNC",
            node_id="chair-01",
            boot_id="0000000000000001",
            boot_generation="0000000000000001",
            seq=1,
            uptime_ms=100,
            occupied=True,
            battery_mv=None,
            config_revision=3,
            health_flags=0,
        ),
    )
)


class FakeHandle:
    def __init__(self, command_id, done):
        self.command_id, self._done = command_id, done

    async def wait(self, timeout=None):
        await self._done.wait()
        return type("State", (), {"status": "applied"})()


class FakeClient:
    """Records handler registrations and set_config calls."""

    def __init__(self):
        self.handlers, self.sent = {}, []
        self._done = asyncio.Event()
        for name in (
            "event",
            "state",
            "movement",
            "movement_loss",
            "command_update",
            "command_error",
            "bridge_status",
        ):
            setattr(self, f"on_{name}", self._register(name))

    def _register(self, name):
        def register(callback):
            self.handlers[name] = callback
            return callback

        return register

    async def set_config(self, configuration, *, expires_in_seconds):
        self.sent.append((configuration, expires_in_seconds))
        self._done = asyncio.Event()
        return FakeHandle(f"command-{len(self.sent)}", self._done)

    def finish(self):
        self._done.set()


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client, self.out, self.err = FakeClient(), io.StringIO(), io.StringIO()
        self.console = Console(self.client, out=self.out, err=self.err)
        self.addAsyncCleanup(self.console.close)

    async def test_state_prints_as_a_json_line_and_sets_the_revision(self):
        await self.client.handlers["state"](STATE)
        line = json.loads(self.out.getvalue().splitlines()[-1])
        self.assertEqual(
            (line["kind"], line["message"]["config_revision"]), ("state", 3)
        )
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
        self.assertIn("no revision seen yet", self.err.getvalue())
        self.client.finish()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertIn("finished: applied", self.err.getvalue())
        await self.console.handle("/config radio_interval_s=40")
        sent, _ = self.client.sent[-1]
        self.assertEqual(
            (sent.heartbeat_s, sent.radio_interval_s, sent.revision), (60, 40, 2)
        )

    async def test_invalid_config_sends_nothing(self):
        await self.console.handle("/config radio_open_s=60")
        await self.console.handle("/config movement_enabled=yes")
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.err.getvalue().count("cannot send"), 2)

    async def test_unknown_command_blank_line_and_quit(self):
        self.assertTrue(await self.console.handle("/bogus"))
        self.assertIn("unknown command", self.err.getvalue())
        self.assertTrue(await self.console.handle("   "))
        self.assertFalse(await self.console.handle("/quit"))

    async def test_state_and_status_before_any_is_received(self):
        await self.console.handle("/state")
        await self.console.handle("/status")
        self.assertEqual(self.err.getvalue().count("nothing received yet"), 2)
        self.assertEqual(self.out.getvalue(), "")

    async def test_state_command_reprints_the_last_state(self):
        await self.client.handlers["state"](STATE)
        await self.console.handle("/state")
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], lines[1])

    async def test_event_handler_takes_a_transaction(self):
        event = SensorEvent.model_validate(
            {**STATE.model_dump(), "publication_id": "p-1"}
        )
        await self.client.handlers["event"](event, object())
        line = json.loads(self.out.getvalue())
        self.assertEqual((line["kind"], line["publication_id"]), ("event", "p-1"))

    async def test_revision_never_moves_backwards(self):
        await self.client.handlers["state"](
            STATE.model_copy(
                update={
                    "message": STATE.message.model_copy(update={"config_revision": 5})
                }
            )
        )
        await self.client.handlers["state"](STATE)  # config_revision 3, older
        await self.console.handle("/config")
        sent, _ = self.client.sent[-1]
        self.assertEqual(sent.revision, 6)

    async def test_finish_waits_for_the_pending_command(self):
        await self.console.handle("/config")
        finishing = asyncio.create_task(self.console.finish())
        await asyncio.sleep(0.01)
        self.assertFalse(finishing.done())
        self.assertIn("waiting for the pending command", self.err.getvalue())
        self.client.finish()
        await asyncio.wait_for(finishing, 1)
        self.assertIn("finished: applied", self.err.getvalue())

    async def test_finish_without_a_pending_command_returns(self):
        await asyncio.wait_for(self.console.finish(), 1)


class MainTests(unittest.TestCase):
    def test_invalid_settings_never_print_the_password(self):
        err = io.StringIO()
        code = main(
            [
                "--host",
                "broker.example",
                "--plaintext",
                "--bridge",
                "pi-01",
                "--consumer",
                "backend-01",
            ],
            environ={"CHAIR_MQTT_USERNAME": "u", "CHAIR_MQTT_PASSWORD": "hunter2"},
            err=err,
        )
        self.assertEqual(code, 2)
        self.assertIn("loopback", err.getvalue())
        self.assertNotIn("hunter2", err.getvalue())

    def test_stdin_redirected_from_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = Path(directory) / "commands.txt"
            commands.write_text("/help\n/quit\n")
            with commands.open() as stdin:
                result = subprocess.run(
                    [
                        sys.executable,
                        "tools/mqtt_console.py",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "9",
                        "--plaintext",
                        "--bridge",
                        "pi-01",
                        "--consumer",
                        "backend-01",
                    ],
                    cwd=ROOT,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("/config key=value", result.stderr)


ROOT = Path(__file__).resolve().parents[1]
MAC = "02:00:00:00:00:01"


@unittest.skipUnless(MOSQUITTO, "private Mosquitto broker unavailable")
class ConsoleBrokerTests(unittest.TestCase):
    def start_bridge(self, directory, port):
        bridge = ChairBridge(
            BridgeConfig(
                node_id="chair-01",
                sensor_mac=MAC,
                bridge_id="pi-01",
                consumer_id="backend-01",
                database_path=Path(directory) / "chair.sqlite",
            ),
            BrokerConfig(
                host="127.0.0.1", port=port, client_id="bridge-test", plaintext=True
            ),
            gateway=FakeGateway(),
        ).start()
        self.addCleanup(bridge.stop)
        deadline = time.monotonic() + 5
        while not bridge.health().subscriptions_ready:
            self.assertLess(time.monotonic(), deadline, "bridge not ready")
            time.sleep(0.02)
        return bridge

    def start_console(self, port):
        proc = subprocess.Popen(
            [
                sys.executable,
                "tools/mqtt_console.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--plaintext",
                "--bridge",
                "pi-01",
                "--consumer",
                "backend-01",
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lines = queue.Queue()
        readers = [
            threading.Thread(
                target=lambda s=stream: [lines.put(line) for line in s], daemon=True
            )
            for stream in (proc.stdout, proc.stderr)
        ]
        for reader in readers:
            reader.start()

        def finish():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

        self.addCleanup(finish)
        return proc, lines

    def wait_for(self, lines, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        seen = []
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                break
            seen.append(line)
            try:
                if predicate(json.loads(line)):
                    return
            except ValueError:
                continue
        self.fail(f"no matching line; saw {seen}")

    def test_console_prints_events_and_sends_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            port = start_broker(self, directory)
            bridge = self.start_bridge(directory, port)
            proc, lines = self.start_console(port)
            self.wait_for(lines, lambda v: v["kind"] == "bridge_status")
            state = STATE.message.model_dump()
            state["config_revision"] = 0
            self.assertEqual(bridge.publish(state, source_mac=MAC).status, "accepted")
            self.wait_for(
                lines, lambda v: v["kind"] == "event" and v["message"]["seq"] == 1
            )
            proc.stdin.write("/config radio_interval_s=60 expires=3\n")
            proc.stdin.flush()
            notice = bridge.receive(timeout=10)
            self.assertEqual(notice.disposition, "queued")
            message = notice.request.message
            self.assertEqual((message.radio_interval_s, message.revision), (60, 1))
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=20), 0)

    def test_eof_waits_for_a_pending_config(self):
        with tempfile.TemporaryDirectory() as directory:
            port = start_broker(self, directory)
            bridge = self.start_bridge(directory, port)
            proc, lines = self.start_console(port)
            self.wait_for(lines, lambda v: v["kind"] == "bridge_status")
            proc.stdin.write("/config radio_interval_s=60 expires=3\n")
            proc.stdin.close()
            notice = bridge.receive(timeout=10)
            self.assertIsNotNone(notice)
            self.assertEqual(notice.request.message.radio_interval_s, 60)
            self.assertEqual(proc.wait(timeout=20), 0)
