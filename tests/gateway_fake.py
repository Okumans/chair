"""In-memory GatewayTransport whose gateway side a test plays line by line."""

import json
import queue
import time


class FakeGateway:
    """A GatewayTransport and its one stream; the test sends and expects lines."""

    def __init__(self):
        self._incoming = queue.Queue()
        self._written = queue.Queue()
        self._partial = bytearray()

    # GatewayTransport
    def open(self):
        return self

    # GatewayStream
    def read(self):
        try:
            return self._incoming.get(timeout=0.02)
        except queue.Empty:
            return b""

    def write(self, data):
        self._partial.extend(data)
        while b"\n" in self._partial:
            line, _, rest = self._partial.partition(b"\n")
            self._partial[:] = rest
            self._written.put(json.loads(line))
        return len(data)

    def close(self):
        pass

    # Test side
    def send(self, envelope):
        self._incoming.put(json.dumps(envelope).encode() + b"\n")

    def expect(self, kind, timeout=5):
        """Return the next written message of ``kind``, skipping others."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                value = self._written.get(timeout=remaining)
            except queue.Empty:
                break
            if value["kind"] == kind:
                return value
        raise AssertionError(f"the bridge did not write {kind}")

    def kinds_written(self, duration):
        """Return the kinds of the messages written during ``duration`` seconds."""
        deadline = time.monotonic() + duration
        kinds = []
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                kinds.append(self._written.get(timeout=remaining)["kind"])
            except queue.Empty:
                break
        return kinds
