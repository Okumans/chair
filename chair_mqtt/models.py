"""Strict public configuration and protocol models for the MQTT facade."""

import re
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

Uint32 = Annotated[int, Field(strict=True, ge=0, le=4294967295)]
PositiveUint32 = Annotated[int, Field(strict=True, ge=1, le=4294967295)]
Timestamp = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
NODE_RE = re.compile(r"[A-Za-z0-9_-]{1,15}\Z")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BridgeConfig(StrictModel):
    """One sensor and one bridge, with an explicitly supplied durable database."""

    node_id: str
    sensor_mac: str
    bridge_id: str
    consumer_id: str
    database_path: Path
    allow_local_plaintext: StrictBool = False

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value):
        if not NODE_RE.fullmatch(value):
            raise ValueError("invalid node ID")
        return value

    @field_validator("bridge_id", "consumer_id")
    @classmethod
    def valid_id(cls, value):
        if not ID_RE.fullmatch(value):
            raise ValueError("invalid bridge or consumer ID")
        return value

    @field_validator("sensor_mac")
    @classmethod
    def valid_mac(cls, value):
        if not MAC_RE.fullmatch(value):
            raise ValueError("invalid sensor MAC")
        address = bytes.fromhex(value.replace(":", ""))
        if not any(address) or address[0] & 1:
            raise ValueError("sensor MAC must be nonzero unicast")
        return value.lower()

    @field_validator("database_path", mode="before")
    @classmethod
    def valid_path(cls, value):
        if not isinstance(value, (str, Path)):
            raise ValueError("durable database path required")
        value = Path(value)
        if str(value) in {"", ".", ":memory:"}:
            raise ValueError("durable database path required")
        return value


class ConfigSet(StrictModel):
    """MQTT command template; firmware-assigned transport identity is absent."""

    version: Literal[2]
    type: Literal["CONFIG_SET"]
    node_id: str
    command_id: str
    revision: Uint32
    heartbeat_s: PositiveUint32
    movement_batch_s: PositiveUint32
    movement_enabled: StrictBool
    radio_windows_enabled: StrictBool
    radio_interval_s: PositiveUint32
    radio_open_s: PositiveUint32
    radio_motion_tail_s: PositiveUint32

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value):
        if not NODE_RE.fullmatch(value):
            raise ValueError("invalid node ID")
        return value

    @field_validator("command_id")
    @classmethod
    def valid_uuid(cls, value):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid command UUID") from error
        if not parsed.int or str(parsed) != value:
            raise ValueError("command UUID must be canonical and nonzero")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if self.radio_open_s > self.radio_interval_s:
            raise ValueError("radio opening exceeds interval")
        return self


class ConfigRequest(StrictModel):
    schema_version: Literal[1]
    expires_at_ms: Timestamp
    message: ConfigSet


class BackendReceipt(StrictModel):
    schema_version: Literal[1]
    type: Literal["MQTT_ACK"]
    consumer_id: str
    publication_id: str

    @field_validator("consumer_id")
    @classmethod
    def valid_consumer(cls, value):
        if not ID_RE.fullmatch(value):
            raise ValueError("invalid consumer ID")
        return value

    @field_validator("publication_id")
    @classmethod
    def valid_publication(cls, value):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid publication UUID") from error
        if not parsed.int or str(parsed) != value:
            raise ValueError("publication UUID must be canonical and nonzero")
        return value


class MovementSample(StrictModel):
    offset_ms: Annotated[int, Field(strict=True, ge=0, le=65535)]
    ax: Annotated[int, Field(strict=True, ge=-32768, le=32767)]
    ay: Annotated[int, Field(strict=True, ge=-32768, le=32767)]
    az: Annotated[int, Field(strict=True, ge=-32768, le=32767)]
    gx: Annotated[int, Field(strict=True, ge=-32768, le=32767)]
    gy: Annotated[int, Field(strict=True, ge=-32768, le=32767)]
    gz: Annotated[int, Field(strict=True, ge=-32768, le=32767)]


class SensorIdentity(StrictModel):
    version: Literal[2] = 2
    node_id: str
    boot_id: str
    seq: PositiveUint32

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value):
        if not NODE_RE.fullmatch(value):
            raise ValueError("invalid node ID")
        return value

    @field_validator("boot_id")
    @classmethod
    def valid_boot(cls, value):
        if not re.fullmatch(r"[0-9a-f]{16}", value) or int(value, 16) == 0:
            raise ValueError("boot ID must be a nonzero 16-digit lowercase hex string")
        return value


class SensorTelemetry(SensorIdentity):
    uptime_ms: Uint32
    occupied: StrictBool
    battery_mv: Annotated[int, Field(strict=True, ge=0, le=65534)] | None
    config_revision: Uint32
    health_flags: Annotated[int, Field(strict=True, ge=0, le=3)]


class SeatEvent(SensorTelemetry):
    type: Literal["OCCUPIED", "VACATED", "HEARTBEAT", "BATTERY_LOW"]

    @model_validator(mode="after")
    def matching_occupancy(self):
        if (self.type == "OCCUPIED" and not self.occupied) or (
            self.type == "VACATED" and self.occupied
        ):
            raise ValueError("event contradicts occupancy")
        return self


class StateSync(SensorTelemetry):
    type: Literal["STATE_SYNC"] = "STATE_SYNC"
    boot_generation: str

    @field_validator("boot_generation")
    @classmethod
    def valid_generation(cls, value):
        if (
            not re.fullmatch(r"[0-9a-f]{16}", value)
            or not 1 <= int(value, 16) <= 0x7FFFFFFFFFFFFFFF
        ):
            raise ValueError("invalid boot generation")
        return value


class MovementBatch(SensorTelemetry):
    type: Literal["MOVEMENT_BATCH"] = "MOVEMENT_BATCH"
    batch_start_ms: Uint32
    duration_ms: Uint32
    end_reason: Literal["interval", "capacity", "stopped", "vacant", "disabled"]
    samples: Annotated[list[MovementSample], Field(max_length=75)]

    @model_validator(mode="after")
    def ordered_samples(self):
        if not self.samples and self.end_reason in {"interval", "capacity"}:
            raise ValueError("only a final boundary may have no samples")
        previous = -1
        for sample in self.samples:
            if not previous < sample.offset_ms <= self.duration_ms:
                raise ValueError("sample offsets must be ordered and within the batch")
            previous = sample.offset_ms
        return self


class MovementLoss(SensorTelemetry):
    type: Literal["MOVEMENT_LOSS"] = "MOVEMENT_LOSS"
    dropped_batches: PositiveUint32


class RadioReady(SensorTelemetry):
    type: Literal["READY"] = "READY"
    reason: Literal["seat_occupied", "movement_started", "scheduled"]
    listen_remaining_ms: Annotated[int, Field(strict=True, ge=0, le=4294967294)] | None


class CommandResult(SensorTelemetry):
    type: Literal["COMMAND_RESULT"] = "COMMAND_RESULT"
    command_id: str
    status: Literal[
        "applied",
        "already_applied",
        "revision_conflict",
        "stale_revision",
        "busy",
        "command_conflict",
    ]

    @field_validator("command_id")
    @classmethod
    def valid_command(cls, value):
        return ConfigSet.valid_uuid(value)


class CommandReceipt(SensorIdentity):
    type: Literal["COMMAND_RECEIPT"] = "COMMAND_RECEIPT"
    command_id: str
    status: Literal[
        "received",
        "applied",
        "already_applied",
        "revision_conflict",
        "stale_revision",
        "busy",
        "command_conflict",
    ]

    @field_validator("command_id")
    @classmethod
    def valid_command(cls, value):
        return ConfigSet.valid_uuid(value)


class CommandNotice(StrictModel):
    """Notification of a committed command; the library still manages dispatch."""

    request: ConfigRequest
    disposition: Literal["queued", "expired", "duplicate"]


class BridgeHealth(StrictModel):
    mqtt_connected: bool
    subscriptions_ready: bool
    gateway_connected: bool
    gateway_ready: bool
    storage_ok: bool
    clock_trusted: bool
    pending_publications: int
    quarantined_publications: int
    mqtt_best_effort_dropped: int
    notifications_dropped: int
    reconnect_required: bool
    fatal: str | None
