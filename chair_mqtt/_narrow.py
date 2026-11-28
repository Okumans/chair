"""Internal assertions for JSON fields already checked by schema validation."""

from typing import cast

from .models import JsonObject


def known_object(value: object) -> JsonObject:
    """Return a value that validation already proved is a JSON object."""
    assert isinstance(value, dict), "validated JSON object expected"
    return cast(JsonObject, value)


def known_list(value: object) -> list[object]:
    """Return a value that validation already proved is a JSON array."""
    assert isinstance(value, list), "validated JSON array expected"
    return cast(list[object], value)


def known_str(value: object) -> str:
    """Return a value that validation already proved is a JSON string."""
    assert isinstance(value, str), "validated JSON string expected"
    return value


def known_int(value: object) -> int:
    """Return a value that validation already proved is a JSON integer."""
    assert isinstance(value, int), "validated JSON integer expected"
    return value
