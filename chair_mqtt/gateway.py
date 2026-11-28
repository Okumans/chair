"""The bridge's gateway side: a transport protocol and the loop that drives it."""

import json
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Protocol, cast

from .contract import _unique_object  # pyright: ignore[reportPrivateUsage]  # package-internal JSON hook shared with the MQTT decoder
from .transport import Lines, write_message

if TYPE_CHECKING:
    from .models import JsonObject

LOG = logging.getLogger(__name__)
HELLO_RETRY_S = 1.0
REOPEN_DELAY_S = 1.0


class GatewayStream(Protocol):
    """One open byte-stream connection to the gateway firmware."""

    def read(self) -> bytes:
        """Return the bytes that arrived, waiting at most about 20 ms; may be empty."""
        ...

    def write(self, data: bytes) -> int | None:
        """Write a prefix of ``data`` within about one second; return its length."""
        ...

    def close(self) -> None:
        """Close the connection; the bridge reopens the transport to reconnect."""
        ...


class GatewayTransport(Protocol):
    """How the bridge reaches the gateway; ``SerialTransport`` is the USB one.

    ``open`` and stream methods raise ``OSError`` when the gateway is
    unavailable or disconnects; the bridge then closes the stream and opens a
    new one a second later.
    """

    def open(self) -> GatewayStream:
        """Open a fresh connection to the gateway."""
        ...


class GatewayBridge(Protocol):
    """The bridge operations a ``GatewayLink`` drives; ``ChairBridge`` provides them."""

    def _gateway_opened(self) -> "JsonObject": ...

    def _gateway_closed(self) -> None: ...

    def _gateway_received(self, envelope: "JsonObject") -> object: ...

    def _gateway_next(self) -> "JsonObject | None": ...

    def _gateway_write(
        self, message: "JsonObject", stream: GatewayStream
    ) -> object: ...


def decode_gateway_line(line: bytes | str) -> "JsonObject":
    """Parse one line from the gateway firmware into an envelope dictionary.

    MQTT payloads forbid floats, but gateway diagnostics such as the hello's
    ``tx_power_dbm`` are fractional. Telemetry fields are still type-checked
    by the protocol validator, and duplicate keys remain ambiguous.
    """
    value = json.loads(line, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("gateway line is not a JSON object")
    return cast("JsonObject", value)


class GatewayLink:
    """Run one transport through the bridge's protocol worker on its own thread."""

    def __init__(self, bridge: GatewayBridge, transport: GatewayTransport) -> None:
        self.bridge, self.transport = bridge, transport
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="chair-gateway", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("gateway link did not stop")

    def _run(self) -> None:
        while not self._stop.is_set():
            stream: GatewayStream | None = None
            opened = False
            try:
                stream = self.transport.open()
                hello: JsonObject | None = self.bridge._gateway_opened()  # pyright: ignore[reportPrivateUsage]  # the bridge's package-internal gateway hook
                opened = True
                write_message(stream, hello)
                hello_at = time.monotonic()
                lines = Lines()
                while not self._stop.is_set():
                    for line in lines.feed(stream.read()):
                        try:
                            envelope = decode_gateway_line(line)
                            self.bridge._gateway_received(envelope)  # pyright: ignore[reportPrivateUsage]  # the bridge's package-internal gateway hook
                            if (
                                hello is not None
                                and envelope.get("kind") == "hello"
                                and envelope.get("request_id") == hello["request_id"]
                            ):
                                hello = None
                        except (
                            ValueError,
                            TypeError,
                            AttributeError,
                            sqlite3.Error,
                        ) as error:
                            LOG.warning("Gateway input was not accepted: %s", error)
                    if (
                        hello is not None
                        and time.monotonic() - hello_at >= HELLO_RETRY_S
                    ):
                        write_message(stream, hello)
                        hello_at = time.monotonic()
                    outgoing = self.bridge._gateway_next()  # pyright: ignore[reportPrivateUsage]  # the bridge's package-internal gateway hook
                    if outgoing:
                        self.bridge._gateway_write(outgoing, stream)  # pyright: ignore[reportPrivateUsage]  # the bridge's package-internal gateway hook
            except (OSError, sqlite3.Error) as error:
                # pyserial's SerialException is an OSError.
                if not self._stop.is_set():
                    LOG.warning("Gateway disconnected or unavailable: %s", error)
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                if opened:
                    try:
                        self.bridge._gateway_closed()  # pyright: ignore[reportPrivateUsage]  # the bridge's package-internal gateway hook
                    except RuntimeError:
                        pass
            self._stop.wait(REOPEN_DELAY_S)
