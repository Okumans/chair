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
from unittest import mock

from chair_mqtt import protocol_bridge as protocol
from chair_mqtt import transport

ROOT = Path(__file__).resolve().parents[1]

MAPPING = {"chair-01": "02:00:00:00:00:01"}
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
CONFIG = dict(
    revision=1,
    heartbeat_s=900,
    movement_batch_s=3,
    movement_enabled=True,
    radio_windows_enabled=True,
    radio_interval_s=30,
    radio_open_s=5,
    radio_motion_tail_s=10,
)


def event(kind="STATE_SYNC", boot="0000000000000001", seq=1, **fields):
    message = dict(
        version=2,
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
        # Most tests supply event times explicitly; blocking-time tests advance this clock.
        self.bridge.monotonic_clock = lambda: 0

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def confirm_ready(self, now, seq, remaining=5000, monotonic=None):
        """Exercise the actual Pi-to-sensor challenge and correlated proof."""
        probe = self.bridge.next(now, monotonic=monotonic)
        self.assertEqual(probe["kind"], "ready_probe")
        self.bridge.receive(
            {"kind": "tx_result", "request_id": probe["request_id"]}, now
        )
        control = probe["message"]
        proof = event(
            "READY_PROOF",
            seq=seq,
            listen_remaining_ms=remaining,
            ack_boot_id=control["boot_id"],
            ack_seq=control["seq"],
        )
        self.bridge.receive(envelope(proof), now, monotonic=monotonic)

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

    def test_v2_snapshot_requires_a_nonzero_generation(self):
        for generation in (None, "0000000000000000"):
            with self.assertRaises(ValueError):
                self.bridge.receive(envelope(event(boot_generation=generation)), 1)
        self.assertFalse(self.bridge.acks)

    def test_old_schema_is_rejected_without_destroying_facts(self):
        self.store.accept(event(), 1)
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 200, 1)
        self.store.db.execute("PRAGMA user_version=0")
        with self.assertRaisesRegex(ValueError, "reset"):
            protocol.Store(str(Path(self.directory.name) / "bridge.sqlite"))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
        )
        self.assertEqual(len(self.store.listing(2)), 1)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 0)

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
        self.confirm_ready(101, 2, None)
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

    def test_policy_result_requires_fresh_ready(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=10000)), 101
        )
        self.confirm_ready(101, 2, 10000)
        self.assertEqual(self.bridge.next(101)["kind"], "command")
        self.bridge.receive(
            envelope(
                event("COMMAND_RESULT", seq=3, command_id=COMMAND_ID, status="applied")
            ),
            102,
        )
        self.assertNotIn("chair-01", self.bridge.available)
        # A delayed or duplicate pre-result announcement cannot recreate a lease.
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=10000)),
            102.5,
        )
        self.assertNotIn("chair-01", self.bridge.available)
        self.bridge.receive(
            envelope(
                event("READY", seq=4, reason="scheduled", listen_remaining_ms=5000)
            ),
            103,
        )
        # An outstanding ACK takes precedence; simulate its completion, then prove freshness.
        ack = self.bridge.next(103)
        self.assertEqual(ack["kind"], "event_ack")
        self.bridge.receive({"kind": "tx_result", "request_id": ack["request_id"]}, 103)
        self.store.queue(
            "chair-01", "22222222-2222-4222-8222-222222222222", CONFIG, 500, 103
        )
        self.confirm_ready(103, 5)
        self.assertEqual(self.bridge.available_until("chair-01"), 108)
        self.bridge.receive(
            envelope(
                event("COMMAND_RESULT", seq=3, command_id=COMMAND_ID, status="applied")
            ),
            104,
        )
        self.assertEqual(self.bridge.available_until("chair-01"), 108)

    def test_radio_configuration_boundaries(self):
        for field in ("radio_interval_s", "radio_open_s", "radio_motion_tail_s"):
            self.assertFalse(protocol.configuration(dict(CONFIG, **{field: 0})))
            self.assertFalse(protocol.configuration(dict(CONFIG, **{field: True})))
        self.assertTrue(protocol.configuration(dict(CONFIG, radio_open_s=30)))
        self.assertFalse(protocol.configuration(dict(CONFIG, radio_open_s=31)))
        self.assertFalse(
            protocol.configuration(
                dict(CONFIG, radio_windows_enabled=False, radio_open_s=31)
            )
        )
        self.assertTrue(
            protocol.configuration(
                dict(
                    CONFIG,
                    radio_interval_s=protocol.UINT32_MAX,
                    radio_open_s=protocol.UINT32_MAX,
                    radio_motion_tail_s=protocol.UINT32_MAX,
                )
            )
        )

    def test_ready_lease_uses_monotonic_time(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 1000, 100)
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=5000)),
            100,
            monotonic=10,
        )
        self.confirm_ready(100, 2, monotonic=10)
        # A wall-clock rollback must not extend the five-second lease.
        outgoing = self.bridge.next(90, monotonic=15)
        self.assertNotEqual((outgoing or {}).get("kind"), "command")

    def test_reconnect_requires_matching_gateway_hello(self):
        hello = self.bridge.reset()
        ready = event("READY", seq=5, reason="scheduled", listen_remaining_ms=5000)
        self.assertEqual(
            self.bridge.receive(envelope(ready), 10)["reason"],
            "awaiting_fresh_connection",
        )
        self.bridge.receive(
            {"kind": "hello", "request_id": hello["request_id"] + 1}, 11
        )
        self.assertFalse(self.bridge.available)
        self.bridge.receive({"kind": "hello", "request_id": hello["request_id"]}, 12)
        self.bridge.receive(envelope(dict(ready, seq=6)), 13)
        self.assertNotIn("chair-01", self.bridge.available)
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 13)
        self.confirm_ready(13, 7)
        self.assertEqual(self.bridge.available_until("chair-01"), 18)

    def test_loss_report_sent_before_ready_does_not_invalidate_lease(self):
        self.bridge.receive(
            envelope(event("MOVEMENT_LOSS", seq=6, dropped_batches=1)), 10
        )
        self.bridge.receive(
            envelope(
                event("READY", seq=5, reason="scheduled", listen_remaining_ms=5000)
            ),
            11,
        )
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 11)
        self.confirm_ready(11, 7)
        self.assertEqual(self.bridge.available_until("chair-01"), 16)

    def test_delayed_ready_and_proof_cannot_grant_expired_lease(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        # An old READY is only a hint; it cannot directly enable commands.
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=5000)), 106
        )
        self.assertNotIn("chair-01", self.bridge.available)
        probe = self.bridge.next(106)
        self.assertEqual(probe["kind"], "ready_probe")
        control = probe["message"]
        proof = event(
            "READY_PROOF",
            seq=2,
            listen_remaining_ms=5000,
            ack_boot_id=control["boot_id"],
            ack_seq=control["seq"],
        )
        # Even a proof that was delayed in the radio or USB path expires from probe send.
        self.bridge.receive(envelope(proof), 112)
        self.assertNotIn("chair-01", self.bridge.available)
        # The READY's announced window has also passed, so the bridge stops probing.
        self.assertIsNone(self.bridge.next(112))

    def test_proof_trailing_newer_reliable_event_grants_lease(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=5000)), 100
        )
        probe = self.bridge.next(100)
        self.assertEqual(probe["kind"], "ready_probe")
        self.bridge.receive(
            {"kind": "tx_result", "request_id": probe["request_id"]}, 100
        )
        control = probe["message"]
        # The sensor drains its outbox first, so a newer heartbeat overtakes the proof.
        self.bridge.receive(envelope(event("HEARTBEAT", seq=3)), 101)
        proof = event(
            "READY_PROOF",
            seq=2,
            listen_remaining_ms=5000,
            ack_boot_id=control["boot_id"],
            ack_seq=control["seq"],
        )
        self.bridge.receive(envelope(proof), 101)
        self.assertEqual(self.bridge.available_until("chair-01"), 105)
        self.assertNotIn("chair-01", self.bridge.probes)

    def test_unmatched_proof_is_not_persisted(self):
        proof = event(
            "READY_PROOF",
            seq=2,
            listen_remaining_ms=5000,
            ack_boot_id="00000000000000aa",
            ack_seq=1,
        )
        self.assertEqual(
            self.bridge.receive(envelope(proof), 100)["reason"], "unmatched_ready_proof"
        )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
        )

    def test_expired_ready_hint_stops_probing(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=2000)), 100
        )
        self.assertIsNone(self.bridge.next(102))

    def test_old_result_invalidates_old_lease_but_keeps_newer_ready(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        self.bridge.receive(
            envelope(
                event("READY", seq=1, reason="scheduled", listen_remaining_ms=5000)
            ),
            100,
        )
        self.confirm_ready(100, 2)
        self.bridge.receive(
            envelope(
                event("READY", seq=4, reason="scheduled", listen_remaining_ms=5000)
            ),
            101,
        )
        self.bridge.receive(
            envelope(
                event("COMMAND_RESULT", seq=3, command_id=COMMAND_ID, status="applied")
            ),
            102,
        )
        self.assertNotIn("chair-01", self.bridge.available)
        self.assertEqual(self.bridge.ready_hints["chair-01"].seq, 4)
        self.store.queue(
            "chair-01", "22222222-2222-4222-8222-222222222222", CONFIG, 500, 102
        )
        ack = self.bridge.next(102)
        self.assertEqual(ack["kind"], "event_ack")
        self.bridge.receive({"kind": "tx_result", "request_id": ack["request_id"]}, 102)
        self.confirm_ready(102, 5)
        self.assertEqual(self.bridge.available_until("chair-01"), 107)

    def test_probe_must_match_and_continuous_mode_has_bounded_proof(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        self.bridge.receive(
            envelope(event("READY", reason="scheduled", listen_remaining_ms=None)), 100
        )
        probe = self.bridge.next(100)
        control = probe["message"]
        proof = event(
            "READY_PROOF",
            seq=2,
            listen_remaining_ms=None,
            ack_boot_id=control["boot_id"],
            ack_seq=control["seq"] + 1,
        )
        self.assertEqual(
            self.bridge.receive(envelope(proof), 101)["reason"], "unmatched_ready_proof"
        )
        self.assertNotIn("chair-01", self.bridge.available)
        proof["ack_seq"] = control["seq"]
        self.bridge.receive(envelope(proof), 102)
        self.assertEqual(self.bridge.available_until("chair-01"), 105)
        self.assertNotEqual(self.bridge.next(105)["kind"], "command")

    def test_usb_validation(self):
        loss = event("MOVEMENT_LOSS", dropped_batches=3)
        self.assertEqual(protocol.validate(envelope(loss), MAPPING), loss)
        for count in (0, -1, True):
            with self.assertRaises(ValueError):
                protocol.validate(
                    envelope(event("MOVEMENT_LOSS", dropped_batches=count)), MAPPING
                )
        for changes in [
            dict(version=1),
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
        missing_listen = event("READY", reason="scheduled")
        with self.assertRaises(ValueError):
            protocol.validate(envelope(missing_listen), MAPPING)
        missing_battery = event()
        del missing_battery["battery_mv"]
        with self.assertRaises(ValueError):
            protocol.validate(envelope(missing_battery), MAPPING)
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

    def test_database_wait_cannot_dispatch_expired_command_or_lease(self):
        for expires, lease in ((500, 15), (105, 50)):
            with self.subTest(expires=expires, lease=lease):
                self.store.db.execute("DELETE FROM commands")
                self.store.db.commit()
                self.store.queue("chair-01", COMMAND_ID, CONFIG, expires, 100)
                self.bridge.reset()
                self.bridge.available["chair-01"] = protocol.Availability(
                    lease, protocol.TelemetryIdentity("0000000000000001", 1, 1)
                )
                clocks = [10, 100]
                self.bridge.monotonic_clock = lambda: clocks[0]
                self.bridge.wall_clock = lambda: clocks[1]
                pending = self.store.pending

                def delayed_pending(now):
                    rows = pending(now)
                    clocks[:] = [16, 106]
                    return rows

                with mock.patch.object(
                    self.store, "pending", side_effect=delayed_pending
                ):
                    self.assertIsNone(self.bridge.next())

    def test_serial_guard_rechecks_after_encoding_and_during_partial_writes(self):
        self.store.queue("chair-01", COMMAND_ID, CONFIG, 500, 100)
        clocks = [10, 100]
        self.bridge.monotonic_clock = lambda: clocks[0]
        self.bridge.wall_clock = lambda: clocks[1]
        self.bridge.available["chair-01"] = protocol.Availability(
            15, protocol.TelemetryIdentity("0000000000000001", 1, 1)
        )
        outgoing = self.bridge.next()
        self.assertEqual(outgoing["kind"], "command")

        class Port:
            def __init__(self):
                self.data = bytearray()
                self.calls = 0

            def write(self, data):
                self.calls += 1
                self.data.extend(data[:3])
                clocks[0] = 16
                return min(3, len(data))

        port = Port()
        dumps = json.dumps

        def delayed_encoding(*args, **kwargs):
            result = dumps(*args, **kwargs)
            clocks[0] = 16
            return result

        with mock.patch.object(transport.json, "dumps", side_effect=delayed_encoding):
            self.assertFalse(self.bridge.send(port, outgoing))
        self.assertEqual(port.calls, 0)
        self.assertIsNone(self.bridge.radio_pending)

        clocks[0] = 10
        outgoing = self.bridge.next()
        with self.assertRaisesRegex(transport.serial.SerialException, "expired"):
            self.bridge.send(port, outgoing)
        self.assertEqual(port.calls, 1)
        self.assertNotIn(b"\n", port.data)
        self.bridge.reset()  # The serial loop reconnects after a partial-line failure.
        self.assertIsNone(self.bridge.command_dispatch)

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
            hello = read_message(master, "hello")
            os.write(master, json.dumps(hello).encode() + b"\n")
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
            hello = read_message(master, "hello")
            os.write(master, json.dumps(hello).encode() + b"\n")
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
