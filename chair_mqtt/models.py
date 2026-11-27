"""Strict public configuration and protocol models for the MQTT library."""

import re
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
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


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BrokerConfig(StrictModel):
    """The MQTT 5 broker that the library connects to and owns a session with.

    TLS verifies the broker certificate and host name before CONNECT, using
    ``ca_file`` or, when it is omitted, the system trust store. ``plaintext``
    disables TLS and is accepted only for a loopback host.
    """

    host: str = Field(min_length=1)
    port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 8883
    client_id: str = Field(min_length=1, max_length=64)
    ca_file: Path | None = None
    username: str | None = None
    password: SecretStr | None = None
    plaintext: StrictBool = False

    @field_validator("ca_file", mode="before")
    @classmethod
    def valid_ca_file(cls, value):
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or str(value) in {"", "."}:
            raise ValueError("CA file must be a path")
        return Path(value)

    @model_validator(mode="after")
    def validate_security(self):
        if self.plaintext and self.host not in LOOPBACK_HOSTS:
            raise ValueError("plaintext MQTT is permitted only to a loopback host")
        if self.plaintext and self.ca_file is not None:
            raise ValueError("a CA file requires TLS; remove plaintext")
        if self.password is not None and self.username is None:
            raise ValueError("a password requires a username")
        return self


class BridgeConfig(StrictModel):
    """One sensor and one bridge, with an explicitly supplied durable database."""

    node_id: str
    sensor_mac: str
    bridge_id: str
    consumer_id: str
    database_path: Path

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


class ClientConfig(StrictModel):
    """Backend/controller identities; the broker is configured by BrokerConfig."""

    node_id: str
    bridge_id: str
    consumer_id: str

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value):
        return BridgeConfig.valid_node(value)

    @field_validator("bridge_id", "consumer_id")
    @classmethod
    def valid_id(cls, value):
        return BridgeConfig.valid_id(value)


class ChairConfiguration(StrictModel):
    """A complete sensor configuration to be submitted through ChairClient."""

    revision: Uint32
    heartbeat_s: PositiveUint32
    movement_batch_s: PositiveUint32
    movement_enabled: StrictBool
    radio_windows_enabled: StrictBool
    radio_interval_s: PositiveUint32
    radio_open_s: PositiveUint32
    radio_motion_tail_s: PositiveUint32

    @model_validator(mode="after")
    def validate_window(self):
        if self.radio_open_s > self.radio_interval_s:
            raise ValueError("radio opening exceeds interval")
        return self


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
    """One raw accelerometer and gyroscope sample within a movement batch."""

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
    """An occupancy, heartbeat, or low-battery sensor event."""

    type: Literal["OCCUPIED", "VACATED", "HEARTBEAT", "BATTERY_LOW"]

    @model_validator(mode="after")
    def matching_occupancy(self):
        if (self.type == "OCCUPIED" and not self.occupied) or (
            self.type == "VACATED" and self.occupied
        ):
            raise ValueError("event contradicts occupancy")
        return self


class StateSync(SensorTelemetry):
    """A sensor's current state and persistent boot generation."""

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
    """A best-effort movement batch with ordered raw sensor samples."""

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
    """A sensor report of movement batches lost before transmission."""

    type: Literal["MOVEMENT_LOSS"] = "MOVEMENT_LOSS"
    dropped_batches: PositiveUint32


class RadioReady(SensorTelemetry):
    """A sensor radio-window readiness announcement."""

    type: Literal["READY"] = "READY"
    reason: Literal["seat_occupied", "movement_started", "scheduled"]
    listen_remaining_ms: Annotated[int, Field(strict=True, ge=0, le=4294967294)] | None


class CommandResult(SensorTelemetry):
    """The sensor's result for a previously received configuration command."""

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
    """A sensor acknowledgement or outcome for a configuration command."""

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


class EventAck(StrictModel):
    """The EVENT_ACK a sensor needs before it discards a reliable event."""

    version: Literal[2]
    type: Literal["EVENT_ACK"]
    node_id: str
    boot_id: str
    seq: PositiveUint32
    ack_boot_id: str
    ack_seq: PositiveUint32


class PublishResult(StrictModel):
    """Outcome of ``ChairBridge.publish`` for one sensor message.

    ``accepted`` and ``duplicate`` messages are durably stored. A reliable
    event, including a retransmitted duplicate, carries the ``ack`` the caller
    must deliver to the sensor; ``ignored`` explains the drop in ``reason``.
    """

    status: Literal["accepted", "duplicate", "ignored"]
    reason: str | None = None
    ack: EventAck | None = None


class BridgeHealth(StrictModel):
    """Current Pi bridge connection, storage, and publication health."""

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
