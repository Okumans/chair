"""Interactive MQTT test console that acts as the backend for a test bridge."""

import argparse
import secrets
from pathlib import Path

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
