import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from chair_mqtt import protocol_bridge as protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import radio_window_bench as bench  # noqa: E402
from test_protocol_bridge import CONFIG, MAPPING, envelope, event  # noqa: E402


class BenchTests(unittest.TestCase):
    def test_rom_boot_lines_are_not_timing_evidence(self):
        for line in (b"ESP-ROM:esp32c3-api1", b"", b"[]", b"null", b'{"partial":'):
            self.assertIsNone(bench.envelope_line(line))
        self.assertEqual(bench.envelope_line(b'{"kind":"hello"}'), dict(kind="hello"))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = protocol.Store(str(Path(self.directory.name) / "test.sqlite"))
        self.addCleanup(self.store.db.close)
        self.bridge = protocol.Bridge(self.store, MAPPING, monotonic_clock=lambda: 0)
        self.log = []

    def make_test(self, mode):
        return bench.FaultTest(mode, self.bridge, "chair-01", CONFIG, self.log.append)

    def receive(self, test, kind, now, seq, **fields):
        fields.setdefault("config_revision", CONFIG["revision"])
        m = event(kind, seq=seq, **fields)
        test.receive(envelope(m), now, now)
        return m

    def proof(self, test, now, seq):
        outgoing = self.bridge.next(now)
        self.assertEqual(outgoing["kind"], "ready_probe")
        self.bridge.receive(
            dict(kind="tx_result", request_id=outgoing["request_id"]), now
        )
        return self.receive(
            test,
            "READY_PROOF",
            now,
            seq,
            listen_remaining_ms=5000,
            ack_boot_id=outgoing["message"]["boot_id"],
            ack_seq=outgoing["message"]["seq"],
        )

    def test_revision_guard_prevents_a_configuration_change(self):
        test = self.make_test("lost-ack")
        self.receive(test, "STATE_SYNC", 100, 1, config_revision=0)
        self.assertEqual(test.outcome, "inconclusive")
        self.assertIsNone(test.command)
        self.assertEqual(self.store.listing(100), [])

    def test_delayed_ready_holds_full_interval_then_only_probes(self):
        test = self.make_test("delayed-ready")
        self.receive(
            test, "READY", 100, 1, listen_remaining_ms=5000, reason="scheduled"
        )
        self.assertIsNone(self.bridge.next(100))
        test.tick(105.99, 105.99)
        self.assertIsNone(test.check_until)
        test.tick(106, 106)
        outgoing = self.bridge.next(106)
        self.assertEqual(outgoing["kind"], "ready_probe")
        self.assertTrue(test.allow(outgoing, 106, 106))
        test.tick(109, 109)
        self.assertEqual(test.outcome, "pass")
        test.cleanup()
        self.assertEqual(self.store.listing(109)[0]["status"], "expired")

    def test_delayed_ready_detects_bad_command_authorization(self):
        test = self.make_test("delayed-ready")
        self.receive(
            test, "READY", 100, 1, listen_remaining_ms=5000, reason="scheduled"
        )
        test.tick(106, 106)
        self.bridge.available["chair-01"] = protocol.Availability(
            111, protocol.TelemetryIdentity("0000000000000001", 1, None)
        )
        outgoing = self.bridge.next(106)
        self.assertEqual(outgoing["kind"], "command")
        self.assertFalse(test.allow(outgoing, 106, 106))
        self.assertEqual(test.outcome, "fail")
        self.assertIsNone(self.bridge.radio_pending)

    def test_fresh_window_is_reported_as_interference(self):
        test = self.make_test("delayed-ready")
        self.receive(
            test, "READY", 100, 1, listen_remaining_ms=5000, reason="scheduled"
        )
        self.receive(
            test, "READY", 101, 2, listen_remaining_ms=5000, reason="scheduled"
        )
        test.tick(106, 106)
        test.tick(109, 109)
        self.assertEqual(test.outcome, "inconclusive")

    def test_lost_ack_waits_for_new_proof_and_old_retry_then_releases_ack(self):
        test = self.make_test("lost-ack")
        self.receive(
            test, "READY", 100, 1, listen_remaining_ms=5000, reason="scheduled"
        )
        self.proof(test, 100, 2)
        outgoing = self.bridge.next(100)
        self.assertEqual(outgoing["kind"], "command")
        self.bridge.receive(
            dict(kind="tx_result", request_id=outgoing["request_id"]), 100
        )
        result = self.receive(
            test,
            "COMMAND_RESULT",
            101,
            3,
            command_id=test.command,
            revision=CONFIG["revision"],
            status="already_applied",
        )
        outgoing = self.bridge.next(101)
        self.assertEqual(outgoing["kind"], "event_ack")
        self.assertFalse(test.allow(outgoing, 101, 101))
        self.receive(
            test, "READY", 130, 4, listen_remaining_ms=5000, reason="scheduled"
        )
        self.proof(test, 130, 5)
        outgoing = self.bridge.next(130)
        self.assertEqual(outgoing["message"]["command_id"], test.second)
        self.assertFalse(test.allow(outgoing, 130, 130))
        test.receive(envelope(result), 131, 131)
        self.assertEqual(test.outcome, "pass")
        outgoing = self.bridge.next(131)
        self.assertTrue(test.allow(outgoing, 131, 131))
        self.assertEqual(outgoing["kind"], "event_ack")
        test.cleanup()
        self.assertEqual(self.store.listing(131)[0]["status"], "applied")
        self.assertEqual(self.store.listing(131)[1]["status"], "expired")
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
        )

    def test_trace_requires_a_complete_single_boot_startup_stop_sequence(self):
        def trace(name, time, **fields):
            return dict(
                kind="sensor_trace",
                boot_id="0000000000000001",
                event=name,
                uptime_ms=time,
                occupied=True,
                radio_state="opening",
                **fields,
            )

        messages = [
            trace("radio_start", 50),
            trace("motion_stop", 380),
            trace("radio_ready", 450, closes_ms=10380),
        ]
        self.assertEqual(bench.trace_tail(messages, 10)["outcome"], "pass")
        bad = copy.deepcopy(messages)
        bad[-1]["closes_ms"] = 5450
        self.assertEqual(bench.trace_tail(bad, 10)["outcome"], "fail")
        for incomplete in (messages[:2], messages[1:], [messages[0], messages[2]]):
            self.assertEqual(
                bench.trace_tail(incomplete, 10)["outcome"], "inconclusive"
            )
        bad[-1]["boot_id"] = "0000000000000002"
        self.assertEqual(bench.trace_tail(bad, 10)["outcome"], "inconclusive")
        # Machine-readable output is suitable for capture with tee.
        json.dumps(bench.trace_tail(messages, 10))


if __name__ == "__main__":
    unittest.main()
