import json
import os
import pty
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import protocol_bridge as protocol  # noqa: E402
import transport  # noqa: E402

MAPPING = {"chair-01": "02:00:00:00:00:01"}
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
CONFIG = dict(revision=1, heartbeat_s=900, movement_batch_s=3, movement_enabled=True)


def event(kind="STATE_SYNC", boot="0000000000000001", seq=1, **fields):
    message = dict(
        version=1,
        type=kind,
        node_id="chair-01",
        boot_id=boot,
        seq=seq,
        uptime_ms=100,
        occupied=True,
        battery_mv=None,
        config_revision=0,
        health_flags=2,
    )
    if kind == "STATE_SYNC":
        message["boot_generation"] = boot
    message.update(fields)
    return message


def envelope(message):
    return dict(kind="telemetry", source_mac=MAPPING["chair-01"], message=message)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = protocol.Store(str(Path(self.directory.name) / "bridge.sqlite"))
        self.bridge = protocol.Bridge(self.store, MAPPING)

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def test_control_identity_rotates_before_sequence_wrap(self):
        previous = self.bridge.boot
        self.bridge.seq = protocol.UINT32_MAX
        control = self.bridge.control(
            "chair-01", "EVENT_ACK", ack_boot_id=previous, ack_seq=1
        )
        self.assertEqual(control["seq"], 1)
        self.assertNotEqual(control["boot_id"], previous)

    def test_requires_durable_database(self):
        for path in ["", ":memory:"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                protocol.Store(path)

    def test_commit_before_ack_duplicate_and_storage_failure(self):
        self.store.db.executescript("""CREATE TRIGGER fail_storage BEFORE INSERT ON events
                                    BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END;""")
        with self.assertRaises(sqlite3.Error):
            self.bridge.receive(envelope(event()), 100)
        self.assertFalse(self.bridge.acks)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
        )
        self.store.db.execute("DROP TRIGGER fail_storage")
        self.assertFalse(self.bridge.receive(envelope(event()), 100)["duplicate"])
        # A separate connection sees the committed event before the ACK can leave the bridge.
        with sqlite3.connect(str(Path(self.directory.name) / "bridge.sqlite")) as other:
            self.assertEqual(
                other.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
            )
        outgoing = self.bridge.next(100)
        self.assertEqual(outgoing["kind"], "event_ack")
        self.assertEqual(outgoing["message"]["ack_seq"], 1)
        duplicate = self.bridge.receive(envelope(event()), 101)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["received_at"], 100)
        self.assertEqual(
            self.store.db.execute("SELECT received_at FROM events").fetchone()[0], 100
        )
        self.assertEqual(self.bridge.next(101)["kind"], "event_ack")
        with self.assertRaises(ValueError):
            self.bridge.receive(envelope(event(occupied=False)), 102)
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(
            self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )

    def test_compact_retransmission_matches_previously_stored_null_fields(self):
        compact = event("OCCUPIED", seq=8)
        previous = dict(
            compact,
            command_id=None,
            status=None,
            boot_generation=None,
            ack_boot_id=None,
            ack_seq=None,
            samples=None,
            listen_remaining_ms=None,
        )
        self.assertFalse(self.bridge.receive(envelope(previous), 100)["duplicate"])
        self.bridge.next(100)

        replay = self.bridge.receive(envelope(compact), 101)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["received_at"], 100)
        self.assertNotIn("command_id", replay["message"])
        self.assertEqual(self.bridge.next(101)["kind"], "event_ack")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
        )

    def test_boot_generation_and_sequence_prevent_state_regression(self):
        self.store.accept(event(), 1)
        self.store.accept(event("VACATED", seq=5, occupied=False), 2)
        self.store.accept(event("OCCUPIED", seq=2), 3)

        def current():
            return json.loads(
                self.store.db.execute("SELECT payload FROM current_state").fetchone()[0]
            )

        self.assertEqual(current()["seq"], 5)
        self.store.accept(event(boot="0000000000000002"), 4)
        self.store.accept(event("VACATED", seq=100, occupied=False), 5)
        self.assertEqual(current()["boot_id"], "0000000000000002")
        self.store.accept(
            event("HEARTBEAT", boot="0000000000000003", seq=200, occupied=False), 6
        )
        self.assertEqual(current()["boot_id"], "0000000000000002")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 6
        )

    def test_unseen_old_boot_cannot_roll_back_current_state(self):
        newest = event(boot="0000000000000500", boot_generation="0000000000000010")
        self.bridge.receive(envelope(newest), 1)
        unseen_old = event(
            boot="0000000000000400", boot_generation="000000000000000f", occupied=False
        )
        self.bridge.receive(envelope(unseen_old), 2)
        self.bridge.receive(
            envelope(dict(unseen_old, type="HEARTBEAT", seq=999, boot_generation=None)),
            3,
        )
        current = json.loads(
            self.store.db.execute("SELECT payload FROM current_state").fetchone()[0]
        )
        self.assertEqual(current["boot_id"], newest["boot_id"])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3
        )
        reused = event(
            boot="0000000000000600", boot_generation=newest["boot_generation"]
        )
        with self.assertRaises(ValueError):
            self.bridge.receive(envelope(reused), 4)
        with self.assertRaises(ValueError):
            self.bridge.receive(
                envelope(dict(newest, seq=2, boot_generation="0000000000000011")), 5
            )

    def test_legacy_snapshots_do_not_establish_current_state(self):
        legacy = event(boot_generation=None)
        self.bridge.receive(envelope(legacy), 1)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM current_state").fetchone()[0], 0
        )
        self.assertEqual(self.bridge.next(1)["kind"], "event_ack")
        replay = dict(legacy, boot_generation="0000000000000000")
        self.assertTrue(self.bridge.receive(envelope(replay), 2)["duplicate"])
        upgraded = event(seq=2)
        self.bridge.receive(envelope(upgraded), 3)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM current_state").fetchone()[0], 1
        )

    def test_ordering_migration_preserves_durable_facts(self):
        self.store.accept(event(), 1)
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 200, 1)
        self.store.db.execute("PRAGMA user_version=0")
        self.store.db.close()
        self.store = protocol.Store(str(Path(self.directory.name) / "bridge.sqlite"))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
        )
        self.assertEqual(len(self.store.listing(2)), 1)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM current_state").fetchone()[0], 0
        )
        self.store.accept(event(seq=2), 2)
        self.assertEqual(
            self.store.db.execute("SELECT generation FROM boots").fetchone()[0], 1
        )

    def test_commands_uuid_conflict_expiry_and_late_result(self):
        for value in [0, 1, protocol.UINT32_MAX]:
            self.assertEqual(
                protocol.configuration(
                    dict(CONFIG, heartbeat_s=value, movement_batch_s=value)
                ),
                value != 0,
            )
        self.assertFalse(protocol.configuration(dict(CONFIG, heartbeat_s=True)))
        with self.assertRaises(ValueError):
            self.store.queue("chair-01", COMMAND_ID, CONFIG, float("inf"), 100)
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 200, 100)
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 200, 100)
        with self.assertRaises(ValueError):
            self.store.queue("chair-01", COMMAND_ID, dict(CONFIG, revision=2), 200, 100)
        self.assertIsNone(self.bridge.next(100))
        self.bridge.receive(
            envelope(event("READY", reason="seat_occupied", listen_remaining_ms=None)),
            101,
        )
        outgoing = self.bridge.next(101)
        self.assertEqual(outgoing["kind"], "command")
        self.assertEqual(outgoing["message"]["command_id"], COMMAND_ID)
        self.assertIsNone(self.bridge.next(101.1))
        self.assertEqual(self.bridge.next(102)["kind"], "command")
        self.assertIsNone(self.bridge.next(200))
        self.assertEqual(self.store.listing(200)[0]["status"], "expired")
        self.bridge.receive(
            envelope(
                event("COMMAND_RESULT", seq=3, command_id=COMMAND_ID, status="applied")
            ),
            201,
        )
        row = self.store.listing(201)[0]
        self.assertEqual(row["status"], "expired")
        self.assertEqual(json.loads(row["result"])["status"], "applied")
        # Availability is ephemeral and is never replayed after reconnect.
        self.bridge.reset()
        self.assertFalse(self.bridge.available)
        self.assertIsNone(self.bridge.next(202))

    def test_usb_validation(self):
        for changes in [
            dict(version=True),
            dict(seq=True),
            dict(seq=0),
            dict(boot_id="0" * 16),
            dict(health_flags=4),
            dict(occupied=1),
            dict(battery_mv=65535),
            dict(type="UNKNOWN"),
            dict(node_id="other"),
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                protocol.validate(envelope(event(**changes)), MAPPING)
        wrong_mac = envelope(event())
        wrong_mac["source_mac"] = "02:00:00:00:00:02"
        with self.assertRaises(ValueError):
            protocol.validate(wrong_mac, MAPPING)
        with self.assertRaises(ValueError):
            protocol.validate(
                envelope(
                    event("READY", reason="movement_started", listen_remaining_ms=-1)
                ),
                MAPPING,
            )
        sample = dict(offset_ms=0, ax=-32768, ay=32767, az=16384, gx=0, gy=0, gz=0)
        batch = event(
            "MOVEMENT_BATCH",
            batch_start_ms=0,
            duration_ms=40,
            end_reason="stopped",
            samples=[sample],
        )
        self.assertEqual(protocol.validate(envelope(batch), MAPPING), batch)
        batch["samples"].append(sample)
        with self.assertRaises(ValueError):
            protocol.validate(envelope(batch), MAPPING)

    def test_gateway_usb_receive_with_real_bridge_acks(self):
        binary = Path(self.directory.name) / "radio-usb-test"
        subprocess.run(
            [
                "c++",
                "-std=c++23",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(ROOT / "tests/radio_fakes"),
                "-I",
                str(ROOT / "shared/include"),
                "-I",
                str(ROOT / "third_party/glaze/include"),
                str(ROOT / "tests/radio_usb_test.cpp"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        lines = []
        for seq in range(1, 1001):
            message = self.bridge.control(
                "chair-01", "EVENT_ACK", ack_boot_id="27a1fb4aa76ba72e", ack_seq=seq
            )
            lines.append(
                json.dumps(
                    {"kind": "event_ack", "message": message, "request_id": seq},
                    separators=(",", ":"),
                )
            )
        subprocess.run(
            [str(binary)], input="\n".join(lines) + "\n", text=True, check=True
        )
        subprocess.run([str(binary), "allocation-failure"], check=True)

    def test_partial_usb_writes(self):
        class Port:
            data = bytearray()

            def write(self, data):
                size = min(len(data), 3)
                self.data.extend(data[:size])
                return size

        port = Port()
        transport.write_message(port, dict(kind="hello"))
        transport.write_message(port, dict(kind="hello"))
        self.assertEqual(port.data, b'{"kind":"hello"}\n{"kind":"hello"}\n')

        class Stuck:
            def write(self, data):
                return 0

        with self.assertRaises(transport.serial.SerialException):
            transport.write_message(Stuck(), {})

    def test_configuration_and_listing_cli(self):
        path = Path(self.directory.name)
        nodes = path / "nodes.json"
        nodes.write_text(json.dumps(MAPPING))
        common = ["--nodes", str(nodes), "--db", str(path / "cli.sqlite")]
        command = [sys.executable, str(ROOT / "tools/transport.py")]
        config_args = [
            "--node",
            "chair-01",
            "--revision",
            "1",
            "--heartbeat-s",
            str(protocol.UINT32_MAX),
            "--movement-batch-s",
            "1",
            "--movement-enabled",
            "false",
            "--expires-at",
            str(time.time() + 3600),
        ]
        submitted = subprocess.run(
            command + ["config"] + common + config_args,
            check=True,
            capture_output=True,
            text=True,
        )
        queued = json.loads(submitted.stdout)
        self.assertEqual(queued["kind"], "queued")
        protocol.canonical_uuid(queued["command_id"])

        listed = subprocess.run(
            command + ["commands"] + common, check=True, capture_output=True, text=True
        )
        row = json.loads(listed.stdout)
        self.assertEqual(row["id"], queued["command_id"])
        settings = json.loads(row["config"])
        self.assertEqual(settings["heartbeat_s"], protocol.UINT32_MAX)
        self.assertFalse(settings["movement_enabled"])

        # Exact UUID/content replay uses the same row, rather than queuing a second command.
        subprocess.run(
            command
            + ["config"]
            + common
            + config_args
            + ["--command-id", queued["command_id"]],
            check=True,
            capture_output=True,
            text=True,
        )
        listed = subprocess.run(
            command + ["commands"] + common, check=True, capture_output=True, text=True
        )
        self.assertEqual(len(listed.stdout.splitlines()), 1)

    def test_serial_reconnect_and_durable_replay(self):
        path = Path(self.directory.name)
        mapping = path / "nodes.json"
        mapping.write_text(json.dumps(MAPPING))
        device = path / "gateway"
        master, slave = pty.openpty()
        device.symlink_to(os.ttyname(slave))
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools/transport.py"),
                "protocol",
                "--db",
                str(path / "live.sqlite"),
                "--nodes",
                str(mapping),
                "--port",
                str(device),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        pending = bytearray()

        def read_message(fd, kind, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    parsed = json.loads(line)
                    if parsed["kind"] == kind:
                        return parsed
                elif select.select([fd], [], [], 0.1)[0]:
                    pending.extend(os.read(fd, 8192))
            self.fail(f"missing USB {kind}")

        try:
            self.assertEqual(read_message(master, "hello"), dict(kind="hello"))
            line = json.dumps(envelope(event())).encode() + b"\n"
            os.write(master, line[:20])
            os.write(master, line[20:])
            self.assertEqual(read_message(master, "event_ack")["message"]["ack_seq"], 1)
            os.close(master)
            os.close(slave)
            master, slave = pty.openpty()
            device.unlink()
            device.symlink_to(os.ttyname(slave))
            pending.clear()
            self.assertEqual(read_message(master, "hello"), dict(kind="hello"))
            # Partial old input must not poison a fresh connection; retry gets another ACK.
            os.write(master, line)
            self.assertEqual(read_message(master, "event_ack")["message"]["ack_seq"], 1)
            with sqlite3.connect(str(path / "live.sqlite")) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
                )
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
