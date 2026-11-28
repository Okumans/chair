"""Offline MQTT-v1 JSON/topic validation; no serial, broker or service side effects."""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from jsonschema import Draft202012Validator, TypeChecker
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import extend  # pyright: ignore[reportUnknownVariableType]  # types-jsonschema leaves extend() unannotated
from pydantic import ValidationError as ModelValidationError

from .models import BackendReceipt, ConfigRequest, JsonObject, MovementSample

MAX_PAYLOAD = 16384
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,32}")
NODE = re.compile(r"[A-Za-z0-9_-]{1,15}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The packaged JSON Schema document; check_schema() below verifies its shape.
SCHEMA: dict[str, Any] = json.loads(
    Path(__file__).with_name("mqtt.schema.json").read_text()
)
Draft202012Validator.check_schema(SCHEMA)


def _is_integer(checker: TypeChecker, value: object) -> bool:
    return type(value) is int


StrictValidator: type[Validator] = extend(  # pyright: ignore[reportUnknownVariableType]  # types-jsonschema leaves extend() unannotated; it returns a validator class
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer),
)

RouteKind = Literal[
    "bridge_status",
    "receipt",
    "event",
    "state",
    "movement",
    "movement_loss",
    "command",
    "command_error",
    "report_status",
    "report_update",
]


class ContractError(ValueError):
    """A bounded packet failed structural, routing or relational validation."""


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    qos: int
    retain: bool
    node: str | None = None
    bridge: str | None = None
    command: str | None = None


def route(topic: object) -> Route:
    """Resolve an exact topic; wildcards and unknown suffixes are not messages."""
    if not isinstance(topic, str) or len(topic.encode("utf-8")) > 256:
        raise ContractError("invalid topic")
    parts = topic.split("/")
    if len(parts) < 5 or parts[:2] != ["smartchair", "v1"]:
        raise ContractError("invalid topic namespace")
    branch, target = parts[2:4]
    suffix = parts[4:]
    if branch == "bridges" and IDENTIFIER.fullmatch(target):
        if suffix in (["status"], ["receipts"]):
            return Route(
                "bridge_status" if suffix == ["status"] else "receipt",
                1,
                suffix == ["status"],
                bridge=target,
            )
    if branch != "nodes" or not NODE.fullmatch(target):
        raise ContractError("invalid topic target")
    simple: dict[tuple[str, ...], tuple[RouteKind, int, bool]] = {
        ("events",): ("event", 1, False),
        ("state",): ("state", 1, True),
        ("movement",): ("movement", 0, False),
        ("movement", "loss"): ("movement_loss", 0, False),
        ("commands", "config"): ("command", 1, False),
        ("command_errors",): ("command_error", 1, False),
    }
    if tuple(suffix) in simple:
        kind, qos, retained = simple[tuple(suffix)]
        return Route(kind, qos, retained, node=target)
    if (
        len(suffix) == 3
        and suffix[0] == "commands"
        and suffix[2] in {"status", "updates"}
    ):
        command = suffix[1]
        if (
            UUID.fullmatch(command)
            and command != "00000000-0000-0000-0000-000000000000"
        ):
            return Route(
                "report_status" if suffix[2] == "status" else "report_update",
                1,
                suffix[2] == "status",
                node=target,
                command=command,
            )
    raise ContractError("unsupported topic")


def unique_object_hook(pairs: list[tuple[str, object]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("duplicate JSON key")
        value[key] = item
    return value


def _non_integer(_: str) -> NoReturn:
    raise ContractError("JSON numeric fields require integer syntax")


def decode(payload: object) -> JsonObject:
    """Reject ambiguous JSON before schema validation, including integral floats."""
    if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
        raise ContractError("invalid payload size/type")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object_hook,
            parse_float=_non_integer,
            parse_constant=_non_integer,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ContractError("invalid or ambiguous JSON") from error
    if not isinstance(value, dict):
        raise ContractError("expected JSON object")
    return cast(JsonObject, value)


# Narrow a JSON value whose type validation has already guaranteed. The
# assertion restates that invariant; it never fails for validated input.
def _object_or_none(value: object) -> JsonObject | None:
    return cast(JsonObject, value) if isinstance(value, dict) else None


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


def validate(
    topic: object,
    payload: bytes,
    *,
    qos: int,
    retain: bool,
    expected_bridge: str | None = None,
    expected_node: str | None = None,
    expected_consumer: str | None = None,
) -> tuple[Route, JsonObject]:
    """Validate producer flags and data; persistent admission/clock checks are separate."""
    target = route(topic)
    if (
        type(qos) is not int
        or qos != target.qos
        or type(retain) is not bool
        or (target.kind != "report_status" and retain != target.retain)
    ):
        raise ContractError("incorrect QoS/retain flags")
    if expected_node is not None and target.node != expected_node:
        raise ContractError("unexpected node")
    if (
        target.bridge
        and expected_bridge is not None
        and target.bridge != expected_bridge
    ):
        raise ContractError("unexpected bridge topic")
    value = decode(payload)
    if target.kind == "command" and len(payload) > 4096:
        raise ContractError("command too large")
    if target.kind == "command_error" and len(payload) > 2048:
        raise ContractError("error report too large")
    validator = StrictValidator(
        {"$ref": "#/$defs/" + target.kind, "$defs": SCHEMA["$defs"]}
    )
    try:
        validator.validate(value)  # pyright: ignore[reportArgumentType]  # decoded JSON; the stub's recursive JSON alias rejects dict[str, object]
    except ValidationError as error:
        raise ContractError(
            "payload does not match " + target.kind + " schema"
        ) from error
    try:
        if target.kind == "command":
            ConfigRequest.model_validate(value)
        elif target.kind == "receipt":
            BackendReceipt.model_validate(value)
        elif target.kind == "movement":
            for sample in known_list(known_object(value["message"])["samples"]):
                MovementSample.model_validate(sample)
    except ModelValidationError as error:
        raise ContractError("payload fails strict message validation") from error
    if (
        expected_bridge is not None
        and "bridge_id" in value
        and value["bridge_id"] != expected_bridge
    ):
        raise ContractError("unexpected publication bridge")
    if target.bridge and "bridge_id" in value and value["bridge_id"] != target.bridge:
        raise ContractError("topic/body bridge mismatch")
    if target.kind == "receipt" and expected_consumer is not None:
        if value["consumer_id"] != expected_consumer:
            raise ContractError("unexpected receipt consumer")
    packet = value.get("message")
    message = _object_or_none(packet)
    if message is not None and message.get("node_id") != target.node:
        raise ContractError("topic/body node mismatch")
    if "source_mac" in value:
        mac = bytes.fromhex(known_str(value["source_mac"]).replace(":", ""))
        if not any(mac) or mac[0] & 1:
            raise ContractError("source MAC must be nonzero unicast")
    if target.kind == "command":
        command = known_object(packet)
        if known_int(command["radio_open_s"]) > known_int(command["radio_interval_s"]):
            raise ContractError("radio opening exceeds interval")
    if message is not None and message.get("type") == "STATE_SYNC":
        if value.get("boot_generation") != message["boot_generation"]:
            raise ContractError("generation metadata contradicts STATE_SYNC")
    if target.kind == "movement":
        batch = known_object(packet)
        previous = -1
        for sample in known_list(batch["samples"]):
            offset = known_int(known_object(sample)["offset_ms"])
            if offset <= previous or offset > known_int(batch["duration_ms"]):
                raise ContractError("invalid acquisition offsets")
            previous = offset
    if target.kind in {"report_status", "report_update"}:
        if value["command_id"] != target.command:
            raise ContractError("topic/body command mismatch")
        if (
            packet is not None
            and known_object(packet)["command_id"] != value["command_id"]
        ):
            raise ContractError("result/report command mismatch")
        status = value["status"]
        if status in {"applied", "rejected"}:
            if packet is None or value["late_result"]:
                raise ContractError("terminal status requires timely result evidence")
            applied = known_object(packet)["status"] in {"applied", "already_applied"}
            if applied != (status == "applied"):
                raise ContractError("terminal status contradicts sensor result")
        elif status == "expired":
            if value["late_result"] != (packet is not None):
                raise ContractError("late evidence flag contradicts expiry report")
        elif packet is not None or value["late_result"]:
            raise ContractError("nonterminal report cannot contain result evidence")
    return target, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--qos", type=int, required=True)
    parser.add_argument("--retain", action="store_true")
    args = parser.parse_args()
    try:
        target, _ = validate(
            args.topic, args.payload.read_bytes(), qos=args.qos, retain=args.retain
        )
    except (OSError, ContractError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"valid": True, "kind": target.kind}))


if __name__ == "__main__":
    main()
