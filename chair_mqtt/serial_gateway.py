"""Optional serial adapter; MQTT client lifecycle remains application-owned."""

import logging
import sqlite3
import threading
import time

import serial
from transport import BAUD_RATE, Lines, write_message

from .contract import decode

LOG = logging.getLogger(__name__)


class SerialGateway:
    """Own one gateway port and pass data through the facade's protocol engine."""

    def __init__(self, bridge, port):
        if not isinstance(port, str) or not port:
            raise ValueError("an explicit serial port is required")
        self.bridge, self.port = bridge, port
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="chair-serial-gateway", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("serial gateway did not stop")

    def _run(self):
        while not self._stop.is_set():
            opened = False
            try:
                with serial.Serial(
                    self.port, BAUD_RATE, timeout=0.02, write_timeout=1, exclusive=True
                ) as port:
                    hello = self.bridge.gateway_opened()
                    opened = True
                    write_message(port, hello)
                    hello_at = time.monotonic()
                    lines = Lines()
                    while not self._stop.is_set():
                        chunk = port.read(min(8192, max(1, port.in_waiting)))
                        for line in lines.feed(chunk):
                            try:
                                envelope = decode(line)
                                self.bridge.gateway_received(envelope)
                                if (
                                    hello is not None
                                    and envelope.get("kind") == "hello"
                                    and envelope.get("request_id")
                                    == hello["request_id"]
                                ):
                                    hello = None
                            except (
                                ValueError,
                                TypeError,
                                AttributeError,
                                sqlite3.Error,
                            ) as error:
                                LOG.warning("Gateway input was not accepted: %s", error)
                        if hello is not None and time.monotonic() - hello_at >= 1:
                            write_message(port, hello)
                            hello_at = time.monotonic()
                        outgoing = self.bridge.gateway_next()
                        if outgoing:
                            self.bridge.gateway_write(outgoing, port)
            except (serial.SerialException, OSError, sqlite3.Error) as error:
                if not self._stop.is_set():
                    LOG.warning("Gateway disconnected or unavailable: %s", error)
            finally:
                if opened:
                    try:
                        self.bridge.gateway_closed()
                    except RuntimeError:
                        pass
            self._stop.wait(1)
