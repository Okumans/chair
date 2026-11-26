#!/usr/bin/env python3
# /// script
# dependencies = ["pyserial==3.5"]
# ///
"""Interactive USB/ESP-NOW bench console; the implementation is chair_mqtt.transport."""

import sys
from pathlib import Path

# Run from a checkout with only pyserial installed; importing chair_mqtt.transport
# does not load the MQTT dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chair_mqtt.transport import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
