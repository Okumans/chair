#!/usr/bin/env python3
"""Interactive MQTT test console for a test bridge; the implementation is chair_mqtt.console.

Run from the checkout with the project dependencies: uv run tools/mqtt_console.py --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chair_mqtt.console import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
