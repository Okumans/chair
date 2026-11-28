"""Interactive MQTT test console that acts as the backend for a test bridge."""

from .models import ChairConfiguration

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
