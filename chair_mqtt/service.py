"""Run the gateway bridge as a standalone service using a TOML configuration."""

import argparse
import logging
import os
import signal
import threading
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .bridge import ChairBridge
from .models import BridgeConfig, BrokerConfig
from .serial_gateway import SerialTransport

LOG = logging.getLogger(__name__)


# ``settings`` is a parsed TOML table; BrokerConfig validates its values.
def broker_config(
    settings: Mapping[str, Any], environ: Mapping[str, str]
) -> BrokerConfig:
    """Build BrokerConfig from the ``[mqtt]`` table and environment credentials."""
    if {"username", "password"} & settings.keys():
        raise ValueError(
            "set broker credentials in CHAIR_MQTT_USERNAME and CHAIR_MQTT_PASSWORD, "
            "not in the configuration file"
        )
    return BrokerConfig(
        **settings,
        username=environ.get("CHAIR_MQTT_USERNAME") or None,
        password=environ.get("CHAIR_MQTT_PASSWORD") or None,  # pyright: ignore[reportArgumentType]  # pydantic validates the str into SecretStr
    )


def main(argv: Sequence[str] | None = None) -> None:
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

    def _stop(*_: object) -> None:
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    # The bridge connects in the background and reconnects by itself, so sensor
    # facts are acknowledged and stored durably while the broker is unreachable.
    gateway = SerialTransport(settings["gateway"]["port"])
    with ChairBridge(config, broker, gateway=gateway) as bridge:
        while not stopping.wait(1):
            health = bridge.health()
            if health.fatal or not health.storage_ok:
                # Reconnecting cannot fix these; exit so systemd restarts the service.
                raise RuntimeError(f"bridge requires restart: {health}")


if __name__ == "__main__":
    main()
