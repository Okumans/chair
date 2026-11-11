#!/usr/bin/env python3
# /// script
# dependencies = ["pyserial==3.5"]
# ///
"""Interactive USB/ESP-NOW bench console for either node (Linux/Pi)."""

import argparse
import json
import os
import re
import selectors
import sys

import serial
from serial.tools import list_ports

MAX_PAYLOAD = 1470
MAX_LINE = 8192


class Lines:
    """Bound partial input and discard overlong lines through the next newline."""

    def __init__(self):
        """Start a new bounded serial input accumulator."""
        self.pending = bytearray()
        self.discarding = False

    def feed(self, data):
        """Return completed lines while retaining partial input and discarding oversized lines."""
        result = []
        for byte in data:
            if byte == 10:
                if not self.discarding and self.pending:
                    result.append(bytes(self.pending).removesuffix(b"\r"))
                self.pending.clear()
                self.discarding = False
            elif not self.discarding:
                if len(self.pending) == MAX_LINE:
                    self.pending.clear()
                    self.discarding = True
                else:
                    self.pending.append(byte)
        return result


def decode_hex(text):
    """Decode 1–1470 binary bytes from contiguous hexadecimal text."""
    if not isinstance(text, str) or not re.fullmatch(r"[0-9a-fA-F]{2,2940}", text):
        raise ValueError("expected 1–1470 bytes of hex without spaces")
    if len(text) % 2:
        raise ValueError("hex must contain complete bytes")
    return bytes.fromhex(text)


def command(text, request_id):
    """Build a diagnostic USB send envelope from text or the /hex command."""
    data = decode_hex(text[5:]) if text.startswith("/hex ") else text.encode("utf-8")
    if not 1 <= len(data) <= MAX_PAYLOAD:
        raise ValueError("payload must be 1–1470 bytes")
    return {"kind": "tx", "request_id": request_id, "data_hex": data.hex()}


def received(line):
    """Validate a diagnostic USB envelope and decode received bytes for display."""
    message = json.loads(line)
    if not isinstance(message, dict) or message.get("kind") not in {
        "hello",
        "rx",
        "tx_result",
        "error",
        "sleep",
        "telemetry",
    }:
        raise ValueError("unknown USB message")
    if message["kind"] == "rx":
        data = decode_hex(message.get("data_hex"))
        try:
            message["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return message


def write_message(port, message):
    """Finish one bounded JSON line through partial writes; errors require reconnect."""
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) - 1 > MAX_LINE:
        raise ValueError("USB message exceeds line limit")
    offset = 0
    while offset < len(data):
        count = port.write(data[offset:])
        if not count:
            raise serial.SerialException("USB write made no progress")
        offset += count


def main():
    """Run the bench console or route explicit protocol, config and command-list operations."""
    if len(sys.argv) > 1 and sys.argv[1] in {"protocol", "config", "commands"}:
        from protocol_bridge import main as protocol_main

        return protocol_main(sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "port", nargs="?", help="USB serial path, preferably /dev/serial/by-id/…"
    )
    parser.add_argument("--list", action="store_true", help="list serial devices")
    args = parser.parse_args()
    if args.list:
        for port in list_ports.comports():
            print(f"{port.device}\t{port.description}\t{port.serial_number or ''}")
        return 0
    if not args.port:
        parser.error("provide a serial port or --list")

    print(
        "Type text or /hex 00ff010a to send; /quit to exit. Payload limit: 1470 bytes.",
        file=sys.stderr,
    )
    request_id = 0
    radio_pending = None
    usb_lines = Lines()
    input_lines = Lines()
    try:
        with serial.Serial(
            args.port, 115200, timeout=0, write_timeout=1, exclusive=True
        ) as port:
            with selectors.SelectSelector() as selector:
                selector.register(port.fileno(), selectors.EVENT_READ, "usb")
                selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
                write_message(port, {"kind": "hello"})
                while True:
                    for key, _ in selector.select():
                        if key.data == "usb":
                            chunk = port.read(max(1, port.in_waiting))
                            for line in usb_lines.feed(chunk):
                                try:
                                    message = received(line)
                                except (ValueError, UnicodeDecodeError) as error:
                                    print(
                                        f"Ignoring malformed USB line: {error}",
                                        file=sys.stderr,
                                    )
                                    continue
                                if (
                                    message["kind"] in {"tx_result", "error"}
                                    and message.get("request_id") == radio_pending
                                ):
                                    radio_pending = None
                                if message["kind"] == "hello":
                                    radio_pending = None
                                print(
                                    json.dumps(message, ensure_ascii=True), flush=True
                                )
                        else:
                            chunk = os.read(sys.stdin.fileno(), 4096)
                            if not chunk:
                                return 0
                            for line in input_lines.feed(chunk):
                                try:
                                    text = line.decode("utf-8")
                                    if text == "/quit":
                                        return 0
                                    if radio_pending is not None:
                                        print(
                                            "Previous radio send is still pending; try again after tx_result.",
                                            file=sys.stderr,
                                        )
                                        continue
                                    request_id = request_id % 0xFFFFFFFF + 1
                                    message = command(text, request_id)
                                except (ValueError, UnicodeDecodeError) as error:
                                    print(f"Cannot send: {error}", file=sys.stderr)
                                    continue
                                write_message(port, message)
                                radio_pending = request_id
    except KeyboardInterrupt:
        return 0
    except (serial.SerialException, OSError) as error:
        print(
            f"USB connection failed: {error}. Reconnect and run the console again.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
