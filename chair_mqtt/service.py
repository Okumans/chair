"""Run the gateway bridge as a standalone service using a TOML configuration."""

import argparse
import logging
import os
import signal
import threading
import tomllib
from pathlib import Path

from .facade import ChairMqttBridge
from .models import BridgeConfig, BrokerConfig
from .serial_gateway import SerialTransport

LOG = logging.getLogger(__name__)


def broker_config(settings, environ):
    """Build BrokerConfig from the ``[mqtt]`` table and environment credentials."""
    if {"username", "password"} & settings.keys():
        raise ValueError(
            "set broker credentials in CHAIR_MQTT_USERNAME and CHAIR_MQTT_PASSWORD, "
            "not in the configuration file"
        )
    return BrokerConfig(
        **settings,
        username=environ.get("CHAIR_MQTT_USERNAME") or None,
        password=environ.get("CHAIR_MQTT_PASSWORD") or None,
    )


def main(argv=None):
    """Load ``--config`` TOML and run the Pi bridge until shutdown or failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with args.config.open("rb") as source:
        settings = tomllib.load(source)
    config = BridgeConfig(**settings["bridge"])
    broker = broker_config(settings["mqtt"], os.environ)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    # The bridge connects in the background and reconnects by itself, so sensor
    # facts are acknowledged and stored durably while the broker is unreachable.
    gateway = SerialTransport(settings["gateway"]["port"])
    with ChairMqttBridge(config, broker, gateway=gateway) as bridge:
        while not stopping.wait(1):
            health = bridge.health()
            if health.fatal or not health.storage_ok:
                # Reconnecting cannot fix these; exit so systemd restarts the service.
                raise RuntimeError(f"bridge requires restart: {health}")


if __name__ == "__main__":
    main()
