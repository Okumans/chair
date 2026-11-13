#!/usr/bin/env python3
# /// script
# dependencies = ["pyserial==3.5"]
# ///
"""Repeatable radio-window fault injection and sensor USB trace capture."""

import argparse
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

import serial
from protocol_bridge import (
    SERIAL_POLL_S,
    SERIAL_WRITE_TIMEOUT_S,
    Bridge,
    Store,
    configuration,
    mapping_file,
    validate,
)
from transport import BAUD_RATE, Lines, write_message


def envelope_line(line):
    """Ignore ROM boot text without treating it as a protocol message or timing evidence."""
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class FaultTest:
    """Inject faults around the real bridge, preserving its deadline guards and durable ACKs."""

    def __init__(self, mode, bridge, node, config, emit):
        self.mode, self.bridge, self.node = mode, bridge, node
        self.config, self.emit = config, emit
        self.command = None
        self.second = None
        self.held = None
        self.release_at = None
        self.check_until = None
        self.target = None
        self.proof_seen = False
        self.interference = False
        self.outcome = None
        self.cleanup_submitted = False

    def finish(self, outcome, reason):
        if self.outcome is None:
            self.outcome = outcome
            self.emit(dict(kind="result", outcome=outcome, reason=reason))

    def queue(self, now):
        command = str(uuid.uuid4())
        self.bridge.store.queue(self.node, command, self.config, now + 300, now)
        self.emit(dict(kind="queued_test", command_id=command))
        return command

    def receive(self, envelope, now, mono):
        m = None
        if envelope.get("kind") == "telemetry":
            m = validate(envelope, self.bridge.mapping)
        if (
            m
            and m["node_id"] == self.node
            and m["type"] != "COMMAND_RECEIPT"
            and self.bridge.hello_request is None
        ):
            # Equal revision can only return already_applied/revision_conflict. It cannot
            # change settings, even if the supplied config is stale or its fields differ.
            if self.outcome is None:
                if m["config_revision"] != self.config["revision"]:
                    self.finish(
                        "inconclusive", "configuration revision does not match sensor"
                    )
                elif self.command is None and m["occupied"]:
                    self.command = self.queue(now)
            if self.mode == "delayed-ready" and m["type"] == "READY" and self.command:
                if (
                    self.held is None
                    and self.check_until is None
                    and self.outcome is None
                ):
                    remaining = m["listen_remaining_ms"]
                    if remaining is None:
                        self.finish("inconclusive", "requires a finite radio window")
                    else:
                        self.held = envelope
                        self.release_at = mono + remaining / 1000 + 1
                        self.emit(
                            dict(
                                kind="held_ready",
                                seq=m["seq"],
                                release_at=self.release_at,
                            )
                        )
                        return
                elif self.outcome is None:
                    self.interference = True
            if (
                self.mode == "delayed-ready"
                and self.outcome is None
                and (m["type"] == "READY_PROOF" or not m.get("occupied", True))
            ):
                self.interference = True
        before = self.bridge.available_until(self.node)
        accepted = self.bridge.receive(envelope, now, mono)
        self.emit(dict(kind="rx", envelope=envelope, accepted=accepted))
        if not m or m["node_id"] != self.node or self.mode != "lost-ack":
            return
        if m["type"] == "COMMAND_RESULT" and m["command_id"] == self.command:
            if m["status"] != "already_applied":
                self.finish(
                    "inconclusive", "no-op configuration did not match sensor settings"
                )
                return
            target = (m["node_id"], m["boot_id"], m["seq"])
            if self.target is None:
                self.target = target
                self.second = self.queue(
                    now
                )  # Demand fresh availability; never send this command.
            elif target == self.target and self.proof_seen and before > mono:
                preserved = self.bridge.available_until(self.node) == before
                self.finish(
                    "pass" if preserved else "fail",
                    "older result retry versus newer lease",
                )
        if m["type"] == "READY_PROOF" and self.target is not None:
            if self.bridge.available_until(self.node) > mono and (
                m["boot_id"] == self.target[1] and m["seq"] > self.target[2]
            ):
                self.proof_seen = True

    def tick(self, now, mono):
        if self.held is not None and mono >= self.release_at:
            self.bridge.receive(self.held, now, mono)
            self.held = None
            self.check_until = mono + 3
            self.emit(dict(kind="released_expired_ready"))
        if self.check_until is not None and mono >= self.check_until:
            self.finish(
                "inconclusive" if self.interference else "pass",
                "delayed READY observation completed",
            )

    def allow(self, outgoing, now, mono):
        """Return whether to transmit; suppressed work releases only its own radio request."""
        m = outgoing["message"]
        suppress = False
        if outgoing["kind"] == "command":
            if self.mode == "delayed-ready":
                suppress = True
                if self.check_until is not None and not self.interference:
                    self.finish("fail", "command selected from an expired READY")
                else:
                    self.finish("inconclusive", "unexpected fresh command availability")
            elif m["command_id"] == self.second or self.outcome is not None:
                suppress = True
        if outgoing["kind"] == "event_ack" and self.target is not None:
            target = (m["node_id"], m["ack_boot_id"], m["ack_seq"])
            suppress = target == self.target and self.outcome is None
        if suppress:
            self.bridge.receive(
                dict(kind="error", request_id=outgoing["request_id"]), now, mono
            )
            self.emit(dict(kind="suppressed", message=outgoing))
        return not suppress

    def cleanup(self):
        """Retain facts and command history; expire only this harness's unfinished commands."""
        with self.bridge.store.db:
            for command in (self.command, self.second):
                if command:
                    self.bridge.store.db.execute(
                        "UPDATE commands SET expires=0,status='expired' WHERE id=? AND status IN ('queued','delivered')",
                        (command,),
                    )


def trace_tail(messages, tail_s):
    """Return evidence only for a confirmed stop between startup and readiness."""
    start, stop, boot = None, None, None
    for m in messages:
        if m.get("kind") != "sensor_trace":
            continue
        if m["boot_id"] != boot:
            start, stop = None, None
            boot = m["boot_id"]
        if m["event"] == "radio_start":
            start, stop = m, None
        elif m["event"] == "seat" and not m["occupied"]:
            start, stop = None, None
        elif m["event"] == "motion_start":
            stop = None
        elif m["event"] == "motion_stop" and start is not None:
            if m["occupied"] and m["radio_state"] == "opening":
                stop = m
        elif m["event"] == "radio_ready" and start is not None:
            if (
                stop is not None
                and start["uptime_ms"] <= stop["uptime_ms"] <= m["uptime_ms"]
            ):
                required = stop["uptime_ms"] + tail_s * 1000
                return dict(
                    kind="tail_check",
                    outcome="pass" if m["closes_ms"] >= required else "fail",
                    stop_ms=stop["uptime_ms"],
                    ready_ms=m["uptime_ms"],
                    closes_ms=m["closes_ms"],
                    required_ms=required,
                )
            start, stop = None, None
    return dict(
        kind="tail_check",
        outcome="inconclusive",
        reason="no complete stop-during-startup trace",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["delayed-ready", "lost-ack", "trace"])
    parser.add_argument(
        "--port", required=True, help="explicit gateway port, or sensor port for trace"
    )
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--nodes")
    parser.add_argument("--node")
    parser.add_argument(
        "--db", help="new durable test database; acknowledged facts remain here"
    )
    parser.add_argument(
        "--config",
        help="complete current configuration JSON, including current revision",
    )
    parser.add_argument("--tail-s", type=int, default=10)
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration <= 0 or args.tail_s <= 0:
        parser.error("duration and tail must be positive")

    def emit(message):
        print(json.dumps(dict(host_monotonic=time.monotonic(), **message)), flush=True)

    store, test = None, None
    try:
        if args.mode != "trace":
            if not all((args.nodes, args.node, args.db, args.config)):
                parser.error("fault tests require --nodes, --node, --db and --config")
            mapping = mapping_file(args.nodes)
            config = json.loads(Path(args.config).read_text())
            if args.node not in mapping or not configuration(config):
                parser.error(
                    "expected a mapped node and a complete valid configuration"
                )
            # Never run alongside pending production commands or overwrite a previous test.
            with Path(args.db).open("x"):
                pass
            store = Store(args.db)
            bridge = Bridge(store, mapping)
            test = FaultTest(args.mode, bridge, args.node, config, emit)
        traces = []
        lines = Lines()
        with serial.Serial(
            args.port,
            BAUD_RATE,
            timeout=SERIAL_POLL_S,
            write_timeout=SERIAL_WRITE_TIMEOUT_S,
            exclusive=True,
        ) as port:
            hello = bridge.reset() if test else dict(kind="hello", request_id=1)
            write_message(port, hello)
            last_hello = time.monotonic()
            end = last_hello + args.duration
            cleanup_until = None
            try:
                while time.monotonic() < end:
                    chunk = port.read(min(8192, max(1, port.in_waiting)))
                    for line in lines.feed(chunk):
                        envelope = envelope_line(line)
                        if envelope is None:
                            emit(dict(kind="non_json_line", bytes=len(line)))
                            continue
                        if test:
                            test.receive(envelope, time.time(), time.monotonic())
                        else:
                            emit(envelope)
                            if envelope.get("kind") == "sensor_trace":
                                traces.append(envelope)
                    mono = time.monotonic()
                    if not test:
                        continue
                    test.tick(time.time(), mono)
                    if test.outcome is not None and cleanup_until is None:
                        test.cleanup()
                        cleanup_until = min(end, mono + 5)
                        # Stop withholding the result ACK on every exit, including failures.
                        if test.target:
                            node, boot, seq = test.target
                            bridge.acks[test.target] = bridge.control(
                                node, "EVENT_ACK", ack_boot_id=boot, ack_seq=seq
                            )
                    if bridge.hello_request is not None and mono - last_hello >= 1:
                        write_message(port, hello)
                        last_hello = mono
                    outgoing = bridge.next()
                    if outgoing and test.allow(outgoing, time.time(), time.monotonic()):
                        sent = bridge.send(port, outgoing)
                        emit(dict(kind="tx", message=outgoing, submitted=sent))
                        if sent and test.target and outgoing["kind"] == "event_ack":
                            m = outgoing["message"]
                            if (
                                m["node_id"],
                                m["ack_boot_id"],
                                m["ack_seq"],
                            ) == test.target:
                                test.cleanup_submitted = True
                    if cleanup_until is not None and (
                        mono >= cleanup_until
                        or not test.target
                        or test.cleanup_submitted
                    ):
                        break
            finally:
                if test and test.target and not test.cleanup_submitted:
                    node, boot, seq = test.target
                    control = bridge.control(
                        node, "EVENT_ACK", ack_boot_id=boot, ack_seq=seq
                    )
                    try:
                        write_message(
                            port,
                            dict(
                                kind="event_ack",
                                request_id=bridge.request % 0xFFFFFFFF + 1,
                                message=control,
                            ),
                        )
                        test.cleanup_submitted = True
                    except (serial.SerialException, OSError):
                        pass
        if test:
            test.finish("inconclusive", "test deadline reached")
            return {"pass": 0, "fail": 1, "inconclusive": 2}[test.outcome]
        result = trace_tail(traces, args.tail_s)
        emit(result)
        return {"pass": 0, "fail": 1, "inconclusive": 2}[result["outcome"]]
    except KeyboardInterrupt:
        emit(dict(kind="result", outcome="inconclusive", reason="interrupted"))
        return 2
    except (ValueError, OSError, serial.SerialException, sqlite3.Error) as error:
        emit(dict(kind="result", outcome="inconclusive", reason=str(error)))
        return 2
    finally:
        try:
            if test:
                test.cleanup()
                emit(
                    dict(
                        kind="cleanup",
                        ack_submitted=test.cleanup_submitted,
                        note="submission does not prove application receipt; sensor retries unacknowledged events",
                    )
                )
        finally:
            if store:
                store.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
