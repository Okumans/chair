"""The gateway's UART serial connection as a GatewayTransport."""

import serial

from .transport import BAUD_RATE


class SerialTransport:
    """Reach the gateway firmware over its serial port."""

    def __init__(self, port):
        if not isinstance(port, str) or not port:
            raise ValueError("an explicit serial port is required")
        self.port = port

    def open(self):
        return _SerialStream(
            serial.Serial(
                self.port, BAUD_RATE, timeout=0.02, write_timeout=1, exclusive=True
            )
        )


class _SerialStream:
    def __init__(self, port):
        self._port = port

    def read(self):
        # Read what is buffered, or wait up to the 20 ms timeout for one byte.
        return self._port.read(min(8192, max(1, self._port.in_waiting)))

    def write(self, data):
        return self._port.write(data)

    def close(self):
        self._port.close()
