"""Interactive MQTT test console that acts as the backend for a test bridge."""

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from .async_storage import SQLiteStorage
from .client import ChairClient
from .models import BrokerConfig, ChairConfiguration, ClientConfig

# Firmware defaults; the MQTT contract has no configuration readback.
DEFAULTS = {
    "heartbeat_s": 900,
    "movement_batch_s": 3,
    "movement_enabled": True,
    "radio_windows_enabled": True,
    "radio_interval_s": 30,
    "radio_open_s": 5,
    "radio_motion_tail_s": 10,
}
BOOLEAN_KEYS = {"movement_enabled", "radio_windows_enabled"}
KEYS = {*DEFAULTS, "revision", "expires"}
DEFAULT_EXPIRY_S = 120
HELP = """Commands:
  /config key=value ...  send a full configuration; unnamed fields come from the
                         last one sent, else firmware defaults. Keys: revision,
                         expires (seconds, default 120), heartbeat_s,
                         movement_batch_s, movement_enabled, radio_windows_enabled,
                         radio_interval_s, radio_open_s, radio_motion_tail_s
  /state                 print the last state received
  /status                print the last bridge status received
  /help                  show this help
  /quit                  exit (Ctrl+D also works)"""


def parse_assignments(text):
    """Parse ``key=value`` tokens into typed configuration overrides."""
    values = {}
    for token in text.split():
        key, separator, raw = token.partition("=")
        if not separator or key not in KEYS:
            raise ValueError(f"expected key=value with a known key, got {token!r}")
        if key in BOOLEAN_KEYS:
            if raw not in {"true", "false"}:
                raise ValueError(f"{key} must be true or false, got {raw!r}")
            values[key] = raw == "true"
        elif raw.isdecimal():
            values[key] = int(raw)
        else:
            raise ValueError(f"{key} must be a non-negative integer, got {raw!r}")
    return values


def build_configuration(base, known_revision, assignments):
    """Return ``(configuration, expiry seconds, revision guessed)``.

    Unnamed fields come from ``base``. The revision is the explicit one, else
    the known revision plus one, else 1, which is reported as guessed.
    """
    fields = {**base, **assignments}
    expires = fields.pop("expires", DEFAULT_EXPIRY_S)
    if not 1 <= expires <= 86400:
        raise ValueError("expires must be between 1 and 86400 seconds")
    guessed = "revision" not in fields and known_revision is None
    if "revision" not in fields:
        fields["revision"] = 1 if guessed else known_revision + 1
    return ChairConfiguration(**fields), expires, guessed


def build_parser():
    """Return the console's argument parser; every setting is explicit."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Broker credentials come from CHAIR_MQTT_USERNAME and "
        "CHAIR_MQTT_PASSWORD. Receipts complete deliveries for --consumer, so "
        "use this only with a test bridge.",
    )
    parser.add_argument("--host", required=True, help="MQTT broker host")
    parser.add_argument(
        "--port", type=int, help="broker port (default 8883, or 1883 with --plaintext)"
    )
    parser.add_argument(
        "--plaintext", action="store_true", help="no TLS; loopback brokers only"
    )
    parser.add_argument("--ca-file", type=Path, help="CA bundle for the broker")
    parser.add_argument("--node", default="chair-01", help="chair node ID")
    parser.add_argument("--bridge", required=True, help="the test bridge's bridge_id")
    parser.add_argument(
        "--consumer", required=True, help="the test bridge's consumer_id"
    )
    parser.add_argument(
        "--db", type=Path, help="receipt database (default: temporary, removed on exit)"
    )
    return parser


def settings_from_args(args, environ):
    """Return ``(BrokerConfig, ClientConfig)``; the MQTT client ID is always new."""
    port = args.port or (1883 if args.plaintext else 8883)
    broker = BrokerConfig(
        host=args.host,
        port=port,
        client_id=f"chair-console-{secrets.token_hex(4)}",
        ca_file=args.ca_file,
        plaintext=args.plaintext,
        username=environ.get("CHAIR_MQTT_USERNAME") or None,
        password=environ.get("CHAIR_MQTT_PASSWORD") or None,
    )
    client = ClientConfig(
        node_id=args.node, bridge_id=args.bridge, consumer_id=args.consumer
    )
    return broker, client


class Console:
    """Print a ChairClient's traffic as JSON lines and run typed commands."""

    def __init__(self, client, *, out, err):
        self.client, self.out, self.err = client, out, err
        self.last_state = self.last_status = None
        self.base = dict(DEFAULTS)
        self.known_revision = None
        self._pending = None

        async def on_event(event, tx):
            self._show("event", event)

        client.on_event(on_event)
        for kind in (
            "state",
            "movement",
            "movement_loss",
            "command_update",
            "command_error",
            "bridge_status",
        ):
            getattr(client, f"on_{kind}")(self._printer(kind))

    def _printer(self, kind):
        async def show(model):
            self._show(kind, model)

        return show

    def _show(self, kind, model):
        if kind == "state":
            self.last_state = model
        elif kind == "bridge_status":
            self.last_status = model
        message = getattr(model, "message", None)
        revision = getattr(message, "config_revision", None)
        if revision is None:
            revision = getattr(model, "config_revision", None)
        if revision is not None:
            self._advance_revision(revision)
        self._line(kind, model)

    def _advance_revision(self, revision):
        # Events can arrive out of order; the firmware rejects older revisions.
        self.known_revision = max(self.known_revision or 0, revision)

    def _line(self, kind, model):
        value = {"kind": kind, **model.model_dump(mode="json")}
        self.out.write(json.dumps(value, ensure_ascii=True) + "\n")
        self.out.flush()

    def _say(self, text):
        self.err.write(text + "\n")
        self.err.flush()

    async def handle(self, line):
        """Run one input line; return False when the console should exit."""
        command, _, rest = line.strip().partition(" ")
        if not command:
            return True
        if command == "/quit":
            if self._pending is not None and not self._pending.done():
                self._say("abandoning the pending command; it may still be sent")
            return False
        if command == "/config":
            await self._config(rest)
        elif command in {"/state", "/status"}:
            kind, last = (
                ("state", self.last_state)
                if command == "/state"
                else ("bridge_status", self.last_status)
            )
            if last is None:
                self._say("nothing received yet")
            else:
                self._line(kind, last)
        elif command == "/help":
            self._say(HELP)
        else:
            self._say("unknown command; type /help")
        return True

    async def _config(self, text):
        if self._pending is not None and not self._pending.done():
            self._say("a configuration is still pending; wait for its result")
            return
        try:
            configuration, expires, guessed = build_configuration(
                self.base, self.known_revision, parse_assignments(text)
            )
        except ValueError as error:
            self._say(f"cannot send: {error}")
            return
        if guessed:
            self._say("no revision seen yet; using revision 1")
        self._say(f"sending {configuration.model_dump_json()}")
        handle = await self.client.set_config(configuration, expires_in_seconds=expires)
        fields = configuration.model_dump()
        self._advance_revision(fields.pop("revision"))
        self.base = fields
        self._pending = asyncio.create_task(self._follow(handle))

    async def _follow(self, handle):
        state = await handle.wait()
        self._say(f"command {handle.command_id} finished: {state.status}")

    async def finish(self):
        """Wait for a pending command to finish; its expiry bounds the wait."""
        if self._pending is not None and not self._pending.done():
            self._say("waiting for the pending command (Ctrl+C to abandon)")
            await asyncio.shield(self._pending)

    async def close(self):
        """Stop following a pending command."""
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            try:
                await self._pending
            except asyncio.CancelledError:
                pass


def _read_lines(stdin):
    """Return a queue fed from ``stdin`` by a daemon thread; None marks the end.

    A thread works for terminals, pipes and files alike and leaves the
    terminal's blocking mode untouched; it never delays exit on Ctrl+C.
    """
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()
    source = getattr(stdin, "buffer", stdin)

    def read():
        try:
            for raw in source:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                loop.call_soon_threadsafe(lines.put_nowait, raw)
        finally:
            loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read, name="console-stdin", daemon=True).start()
    return lines


async def _session(args, broker, client_config, stdin, out, err):
    with tempfile.TemporaryDirectory(prefix="chair-console-") as scratch:
        path = args.db or Path(scratch) / "receipts.sqlite"
        err.write(
            f"connecting to {broker.host}:{broker.port} as {broker.client_id}; "
            f"node {client_config.node_id}, bridge {client_config.bridge_id}, "
            f"consumer {client_config.consumer_id}\n"
            "Receipts from this console complete deliveries for that consumer: "
            "use it only with a test bridge. Type /help for commands.\n"
        )
        err.flush()
        lines = _read_lines(stdin)
        async with ChairClient(client_config, SQLiteStorage(path), broker) as client:
            console = Console(client, out=out, err=err)
            try:
                while (line := await lines.get()) is not None:
                    if not await console.handle(line):
                        break
                else:
                    await console.finish()
            finally:
                await console.close()
    return 0


def main(
    argv=None, *, environ=os.environ, stdin=sys.stdin, out=sys.stdout, err=sys.stderr
):
    """Run the console until /quit, end of input or Ctrl+C."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        broker, client_config = settings_from_args(args, environ)
    except ValueError as error:
        if isinstance(error, ValidationError):
            # The default text repeats the input, including any password.
            reason = "; ".join(e["msg"] for e in error.errors(include_input=False))
        else:
            reason = str(error)
        err.write(f"invalid settings: {reason}\n")
        return 2
    try:
        return asyncio.run(_session(args, broker, client_config, stdin, out, err))
    except KeyboardInterrupt:
        return 0
