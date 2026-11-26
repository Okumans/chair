"""Run the gateway bridge as a standalone service using a TOML configuration."""

import argparse
import logging
import os
import signal
import socket
import threading
import time
import tomllib
from pathlib import Path

import paho.mqtt.client as mqtt

from .facade import ChairMqttBridge
from .models import BridgeConfig

LOG = logging.getLogger(__name__)


def _abort_transport(client):
    """Drop the connection without DISCONNECT; Paho's loop then reconnects."""
    sock = client.socket()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def main(argv=None):
    """Load ``--config`` TOML and run the Pi bridge until shutdown or failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with args.config.open("rb") as source:
        settings = tomllib.load(source)
    config = BridgeConfig(**settings["bridge"])
    broker = settings["mqtt"]
    if not broker.get("ca_file") and not (
        config.allow_local_plaintext
        and broker["host"] in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError(
            "a CA file is required except for explicit loopback development"
        )
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=broker["client_id"],
        protocol=mqtt.MQTTv5,
    )
    if broker.get("ca_file"):
        client.tls_set(ca_certs=broker["ca_file"])
    username = os.environ.get("CHAIR_MQTT_USERNAME")
    if username:
        client.username_pw_set(username, os.environ.get("CHAIR_MQTT_PASSWORD"))
    bridge = ChairMqttBridge(client, config).attach()
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    started = False
    try:
        # Connect in Paho's network thread so an unreachable broker does not stop
        # the gateway; sensor facts are buffered durably until it is reachable.
        client.connect_async(
            broker["host"], broker.get("port", 8883), **bridge.connect_options()
        )
        client.loop_start()
        started = True
        bridge.start_gateway(settings["gateway"]["port"])
        aborted_at = None
        while not stopping.wait(1):
            health = bridge.health()
            if health.fatal or not health.storage_ok:
                raise RuntimeError(f"bridge requires restart: {health}")
            if not health.mqtt_connected:
                aborted_at = None  # Paho's loop is already reconnecting.
            elif health.reconnect_required and (
                aborted_at is None or time.monotonic() - aborted_at >= 30
            ):
                LOG.warning("Reconnecting MQTT to recover unacknowledged deliveries")
                _abort_transport(client)
                aborted_at = time.monotonic()
    finally:
        confirmed = bridge.detach()
        if started:
            if confirmed:
                client.disconnect()
            else:
                sock = client.socket()
                if sock is not None:
                    sock.close()
            client.loop_stop()
            if not confirmed:
                bridge.detach()


if __name__ == "__main__":
    main()
