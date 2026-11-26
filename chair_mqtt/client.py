"""Async application facade for the chair MQTT protocol."""

import asyncio
import inspect
import logging
import math
import ssl
import threading
import time
import uuid
from typing import Literal

import paho.mqtt.client as mqtt
from pydantic import BaseModel, ConfigDict

from .contract import ContractError, route, validate
from .models import (
    ChairConfiguration,
    ClientConfig,
    CommandResult,
    ConfigRequest,
    ConfigSet,
    MovementBatch,
    MovementLoss,
    SeatEvent,
    StateSync,
)

LOG = logging.getLogger(__name__)


class SensorEvent(BaseModel):
    """A reliable sensor publication; ``message`` contains its typed ESP data."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    bridge_id: str
    publication_id: str
    source_mac: str
    received_at_ms: int | None
    boot_generation: str | None
    message: StateSync | SeatEvent | CommandResult


class SensorState(SensorEvent):
    """Last known sensor state; its publication ID is always ``None``."""

    publication_id: None
    boot_generation: str


class BridgeStatus(BaseModel):
    """Bridge availability and optional health details reported over MQTT."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    type: Literal["BRIDGE_STATUS"]
    bridge_id: str
    instance_id: str
    status: Literal["online", "offline"]
    reason: str | None
    observed_at_ms: int | None
    gateway_connected: bool | None = None
    gateway_ready: bool | None = None
    storage_ok: bool | None = None
    subscriptions_ready: bool | None = None
    pending_publications: int | None = None
    overdue_receipts: int | None = None
    quarantined_publications: int | None = None
    mqtt_best_effort_dropped: int | None = None


class CommandError(BaseModel):
    """A bridge admission error, optionally associated with a command ID."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    bridge_id: str
    error_id: str
    command_id: str | None
    reason: str
    retryable: bool
    received_at_ms: int | None


class CommandUpdate(BaseModel):
    """A bridge command report, including any late sensor result."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    type: Literal["COMMAND_STATUS"]
    bridge_id: str
    publication_id: str | None
    command_id: str
    report_seq: int
    status: Literal["queued", "delivered", "applied", "rejected", "expired"]
    requested_revision: int
    expires_at_ms: int
    updated_at_ms: int | None
    late_result: bool
    reason: str | None
    message: CommandResult | None


class CommandState(BaseModel):
    """Latest locally stored command state and, when available, its report."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    command_id: str
    admitted: bool
    status: (
        Literal[
            "queued", "delivered", "applied", "rejected", "expired", "admission_unknown"
        ]
        | None
    )
    report_seq: int
    report: CommandUpdate | None


class CommandHandle:
    """A persisted submission, identified by ``command_id``."""

    def __init__(self, client, command_id):
        self._client, self.command_id = client, command_id

    async def wait(self, timeout=None):
        """Wait for a terminal state; timeout raises TimeoutError without cancellation.

        ``admission_unknown`` is terminal for this wait but may change if a
        late report arrives. Use ``get_command`` or a callback to observe it.
        """

        async def poll():
            while True:
                current = await self._client.get_command(self.command_id)
                if current and current.status in {
                    "applied",
                    "rejected",
                    "expired",
                    "admission_unknown",
                }:
                    return current
                await asyncio.sleep(0.2)

        return await asyncio.wait_for(poll(), timeout)


class ChairClient:
    """Backend/controller interface for a caller-owned Paho MQTT 5 client.

    Provide ``ClientConfig`` and a ``Storage`` connector, register async
    callbacks, then await ``attach`` before connecting the supplied client.
    The application owns connection, network loop and recovery.
    """

    def __init__(self, mqtt_client, config: ClientConfig, storage):
        if not isinstance(config, ClientConfig):
            raise TypeError("config must be ClientConfig")
        self.client, self.config, self.storage = mqtt_client, config, storage
        self._handlers = {
            name: []
            for name in (
                "event",
                "movement",
                "command_update",
                "bridge_status",
                "state",
                "movement_loss",
                "command_error",
            )
        }
        self._old = {}
        self._installed = {}
        self._queue = asyncio.Queue(maxsize=128)
        self._loop = self._task = None
        self._attached = self._connected = self._ready = False
        self._stopping = self._draining = self._ever_connected = False
        self._generation = 0
        self._network_generation = 0
        self._network_lock = threading.Lock()
        self._subscriptions = set()
        self._subscription_deadline = 0.0
        self._flights = {}
        self._inflight_receipts = set()
        self._previous_manual_ack = None
        self.error = None
        self.reconnect_required = False

    def _register(self, name, callback):
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("callbacks must be async functions")
        self._handlers[name].append(callback)
        return callback

    def on_event(self, callback):
        """Register async ``callback(event: SensorEvent, transaction)`` for reliable facts."""
        return self._register("event", callback)

    def on_movement(self, callback):
        """Register async ``callback(batch: MovementBatch)`` for best-effort samples."""
        return self._register("movement", callback)

    def on_command_update(self, callback):
        """Register async ``callback(update: CommandUpdate)`` for status notifications."""
        return self._register("command_update", callback)

    def on_bridge_status(self, callback):
        """Register async ``callback(status: BridgeStatus)`` for bridge availability."""
        return self._register("bridge_status", callback)

    def on_state(self, callback):
        """Register async ``callback(state: SensorState)`` for the last known state."""
        return self._register("state", callback)

    def on_movement_loss(self, callback):
        """Register async ``callback(loss: MovementLoss)`` for dropped sensor batches."""
        return self._register("movement_loss", callback)

    def on_command_error(self, callback):
        """Register async ``callback(error: CommandError)`` for command admission errors."""
        return self._register("command_error", callback)

    async def _emit(self, name, *args):
        for callback in self._handlers[name]:
            await callback(*args)

    def connect_options(self):
        """Return required MQTT 5 options to unpack into the client's ``connect``."""
        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.SessionExpiryInterval = 86400
        properties.ReceiveMaximum = 16
        properties.MaximumPacketSize = 65536
        return {
            "keepalive": 30,
            "clean_start": mqtt.MQTT_CLEAN_START_FIRST_ONLY,
            "properties": properties,
        }

    @property
    def ready(self):
        """Whether all chair subscriptions have been granted at QoS 1."""
        return self._ready and not self.reconnect_required and not self._draining

    @property
    def connected(self):
        """Whether the latest MQTT connection succeeded; check ``ready`` separately."""
        return self._connected

    def _enqueue(self, item):
        def put():
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self.error = "incoming MQTT queue is full"
                self.reconnect_required = True

        self._loop.call_soon_threadsafe(put)

    def _chain(self, name, fn):
        previous = getattr(self.client, name)
        self._old[name] = previous

        def callback(*args):
            fn(*args)
            if previous:
                try:
                    previous(*args)
                except Exception:
                    LOG.exception("Application MQTT callback failed")

        setattr(self.client, name, callback)
        self._installed[name] = callback

    async def attach(self):
        """Initialize storage and install MQTT handlers before the client connects.

        Returns this client. Call once from the event loop used for callbacks.
        Enables manual acknowledgements for the entire supplied Paho client.
        """
        if self._attached or self.client.is_connected():
            raise RuntimeError("attach once before connecting")
        if (
            self.client.callback_api_version != mqtt.CallbackAPIVersion.VERSION2
            or self.client._protocol != mqtt.MQTTv5
        ):
            raise ValueError("Paho MQTT 5 with callback API version 2 is required")
        self._loop = asyncio.get_running_loop()
        await self.storage.initialize(
            self.config.node_id, self.config.bridge_id, self.config.consumer_id
        )
        self._previous_manual_ack = self.client._manual_ack
        self.client.manual_ack_set(True)
        self._chain("on_connect", self._on_connect)
        self._chain("on_disconnect", self._on_disconnect)
        self._chain(
            "on_subscribe", lambda c, u, mid, r, p: self._enqueue(("subscribe", mid, r))
        )
        self._chain(
            "on_publish", lambda c, u, mid, r, p: self._enqueue(("publish", mid, r))
        )
        self._chain(
            "on_unsubscribe",
            lambda c, u, mid, r, p: self._enqueue(("unsubscribe", mid, r)),
        )
        node, bridge = self.config.node_id, self.config.bridge_id
        self._filters = [
            f"smartchair/v1/nodes/{node}/events",
            f"smartchair/v1/nodes/{node}/state",
            f"smartchair/v1/nodes/{node}/movement",
            f"smartchair/v1/nodes/{node}/movement/loss",
            f"smartchair/v1/nodes/{node}/commands/+/updates",
            f"smartchair/v1/nodes/{node}/commands/+/status",
            f"smartchair/v1/nodes/{node}/command_errors",
            f"smartchair/v1/bridges/{bridge}/status",
        ]
        for topic in self._filters:
            self.client.message_callback_add(topic, self._message)
        self._attached = True
        self._task = asyncio.create_task(self._worker())
        return self

    def _message(self, client, userdata, message):
        with self._network_lock:
            generation = self._network_generation
        self._enqueue(
            (
                "message",
                generation,
                message.mid,
                message.qos,
                bool(message.retain),
                message.topic,
                bytes(message.payload),
            )
        )

    def _on_connect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            generation = self._network_generation
        security_error = None
        if not reason.is_failure:
            context = getattr(client, "_ssl_context", None)
            if getattr(client, "_ssl", False):
                if (
                    context is None
                    or context.verify_mode != ssl.CERT_REQUIRED
                    or not context.check_hostname
                    or getattr(client, "_tls_insecure", False)
                ):
                    security_error = "MQTT TLS must verify certificate and hostname"
            elif not (
                self.config.allow_local_plaintext
                and getattr(client, "_host", None) in {"localhost", "127.0.0.1", "::1"}
            ):
                security_error = (
                    "MQTT plaintext requires explicit loopback configuration"
                )
        self._enqueue(("connect", generation, reason, security_error))

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            generation = self._network_generation
        self._enqueue(("disconnect", generation))

    async def set_config(
        self, configuration: ChairConfiguration, *, expires_in_seconds
    ):
        """Persist a full configuration and return a handle without waiting for the sensor.

        ``expires_in_seconds`` must be an integer from 1 through 86400. The
        handle's ID can be stored and later passed to ``get_command``.
        """
        if not self._attached or self._stopping:
            raise RuntimeError("client is not attached")
        if not isinstance(configuration, ChairConfiguration):
            raise TypeError("configuration must be ChairConfiguration")
        if type(expires_in_seconds) is not int or not 1 <= expires_in_seconds <= 86400:
            raise ValueError("expiry must be between 1 and 86400 seconds")
        now = int(time.time() * 1000)
        command_id = str(uuid.uuid4())
        command = ConfigSet(
            version=2,
            type="CONFIG_SET",
            node_id=self.config.node_id,
            command_id=command_id,
            **configuration.model_dump(),
        )
        request = ConfigRequest(
            schema_version=1,
            expires_at_ms=now + expires_in_seconds * 1000,
            message=command,
        )
        await self.storage.enqueue_command(request, now)
        return CommandHandle(self, command_id)

    async def get_command(self, command_id):
        """Return the locally stored CommandState, or None for an unknown ID."""
        await self.storage.due_commands(int(time.time() * 1000))
        value = await self.storage.get_command(command_id)
        if value is None:
            return None
        report = value["report"]
        if report is not None:
            report = CommandUpdate.model_validate({**report, "publication_id": None})
        return CommandState(
            command_id=command_id,
            admitted=value["admitted"],
            status=value["outcome"],
            report_seq=value["report_seq"],
            report=report,
        )

    async def _fact(self, target, value, tx):
        if target.kind == "event":
            await self._emit("event", SensorEvent.model_validate(value), tx)

    async def _incoming(self, generation, mid, qos, retain, topic, payload):
        if (
            generation != self._generation
            or not self._connected
            or self.reconnect_required
            or self._draining
        ):
            return
        try:
            target = route(topic)
            if target.kind == "report_status" and not payload:
                with self._network_lock:
                    still_current = generation == self._network_generation
                if qos == 1 and target.node == self.config.node_id and still_current:
                    self.client.ack(mid, qos)
                return
            target, value = validate(
                topic,
                payload,
                qos=qos,
                retain=retain,
                expected_node=None,
                expected_bridge=self.config.bridge_id,
            )
            if target.node is not None and target.node != self.config.node_id:
                raise ContractError("unexpected node")
            fresh = False
            if target.kind in {"event", "report_update"}:
                fresh = await self.storage.ingest(
                    topic, payload, qos=qos, retain=retain, on_fact=self._fact
                )
            if target.kind in {"report_update", "report_status"}:
                existing = await self.storage.get_command(value["command_id"])
                if existing is not None:
                    changed = await self.storage.observe_report(
                        topic, payload, qos=qos, retain=retain
                    )
                    if changed:
                        await self._emit(
                            "command_update", CommandUpdate.model_validate(value)
                        )
                elif target.kind == "report_update" and fresh:
                    await self._emit(
                        "command_update", CommandUpdate.model_validate(value)
                    )
            elif target.kind == "movement":
                await self._emit(
                    "movement", MovementBatch.model_validate(value["message"])
                )
            elif target.kind == "movement_loss":
                await self._emit(
                    "movement_loss", MovementLoss.model_validate(value["message"])
                )
            elif target.kind == "state":
                await self._emit("state", SensorState.model_validate(value))
            elif target.kind == "bridge_status":
                await self._emit("bridge_status", BridgeStatus.model_validate(value))
            elif target.kind == "command_error":
                await self._emit("command_error", CommandError.model_validate(value))
        except ContractError as error:
            await self.storage.reject(topic, payload, str(error))
        except Exception as error:
            self.error = str(error)
            self.reconnect_required = True
            LOG.exception("MQTT delivery was not acknowledged")
            return
        with self._network_lock:
            still_current = generation == self._network_generation
        if (
            qos == 1
            and generation == self._generation
            and self._connected
            and still_current
        ):
            if self.client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS:
                self.reconnect_required = True

    async def _tick(self):
        if self.reconnect_required:
            return
        if (
            self._connected
            and self._subscriptions
            and time.monotonic() >= self._subscription_deadline
        ):
            raise RuntimeError("MQTT SUBACK timeout; reconnect required")
        if not self._ready or self.reconnect_required or self._draining:
            return
        if any(
            time.monotonic() >= deadline for _, _, deadline in self._flights.values()
        ):
            raise RuntimeError("MQTT PUBACK timeout; reconnect required")
        now = int(time.time() * 1000)
        for command_id, topic, payload, expires in await self.storage.due_commands(now):
            if any(
                kind == "command" and key == command_id
                for kind, key, _ in self._flights.values()
            ):
                continue
            props = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
            props.MessageExpiryInterval = max(1, math.ceil((expires - now) / 1000))
            props.PayloadFormatIndicator = 1
            props.ContentType = "application/json"
            info = self.client.publish(
                topic, payload, qos=1, retain=False, properties=props
            )
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self._flights[info.mid] = ("command", command_id, time.monotonic() + 30)
                await self.storage.command_submitted(command_id, now)
            else:
                raise RuntimeError(f"command publish failed: {info.rc}")
        for publication_id, topic, payload in await self.storage.pending_receipts():
            if publication_id in self._inflight_receipts:
                continue
            props = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
            props.PayloadFormatIndicator = 1
            props.ContentType = "application/json"
            info = self.client.publish(
                topic, payload, qos=1, retain=False, properties=props
            )
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self._flights[info.mid] = (
                    "receipt",
                    publication_id,
                    time.monotonic() + 30,
                )
                self._inflight_receipts.add(publication_id)
            else:
                raise RuntimeError(f"receipt publish failed: {info.rc}")

    async def _worker(self):
        while not self._stopping:
            try:
                item = await asyncio.wait_for(self._queue.get(), 0.25)
            except asyncio.TimeoutError:
                item = None
            try:
                if item:
                    kind, *args = item
                    if kind == "connect":
                        self._generation = args[0]
                        self._connected = not args[1].is_failure
                        self._ever_connected |= self._connected
                        if args[1].is_failure:
                            raise RuntimeError("MQTT connection rejected")
                        if args[2]:
                            raise RuntimeError(args[2])
                        self._ready = False
                        self._subscriptions.clear()
                        self._subscription_deadline = time.monotonic() + 30
                        self._flights = {
                            mid: (kind, key, time.monotonic() + 30)
                            for mid, (kind, key, _) in self._flights.items()
                        }
                        if self._connected:
                            self.error = None
                            self.reconnect_required = False
                            for topic in self._filters:
                                rc, mid = self.client.subscribe(
                                    topic,
                                    options=mqtt.SubscribeOptions(
                                        qos=1, retainAsPublished=True
                                    ),
                                )
                                if rc != mqtt.MQTT_ERR_SUCCESS:
                                    raise RuntimeError("subscription failed")
                                self._subscriptions.add(mid)
                    elif kind == "disconnect":
                        self._generation = args[0]
                        self._connected = self._ready = False
                    elif kind == "subscribe" and args[0] in self._subscriptions:
                        if (
                            len(args[1]) != 1
                            or args[1][0].is_failure
                            or args[1][0].value != 1
                        ):
                            raise RuntimeError("subscription refused or downgraded")
                        self._subscriptions.remove(args[0])
                        self._ready = not self._subscriptions
                    elif kind == "message":
                        await self._incoming(*args)
                    elif kind == "publish":
                        mid, reason = args
                        flight = self._flights.pop(mid, None)
                        if flight:
                            flight_kind, key, _ = flight
                            if flight_kind == "receipt":
                                self._inflight_receipts.discard(key)
                            if reason.is_failure:
                                raise RuntimeError("MQTT publication rejected")
                            if flight_kind == "receipt":
                                await self.storage.confirm_receipt(key)
                    elif (
                        kind == "unsubscribe"
                        and getattr(self, "_unsubscribe_mid", None) == args[0]
                    ):
                        if not self._unsubscribe_result.done():
                            self._unsubscribe_result.set_result(
                                len(args[1]) == len(self._filters)
                                and all(not r.is_failure for r in args[1])
                            )
                await self._tick()
            except Exception as error:
                self.error = str(error)
                self.reconnect_required = True
                LOG.exception("Chair client worker failed")

    async def detach(self):
        """Stop processing and remove subscriptions without disconnecting Paho.

        Return True if it was safe to release handlers. On False, abort and
        discard the MQTT client; if still connected, call detach again after
        aborting to finish closing storage. Do not reuse this facade.
        """
        if not self._attached:
            return False
        self._draining = True
        released = not self._ever_connected
        if self.client.is_connected():
            self._unsubscribe_result = self._loop.create_future()
            rc, self._unsubscribe_mid = self.client.unsubscribe(self._filters)
            if rc == mqtt.MQTT_ERR_SUCCESS:
                try:
                    released = await asyncio.wait_for(self._unsubscribe_result, 5)
                except asyncio.TimeoutError:
                    released = False
            if not released and self.client.is_connected():
                return False
        self._stopping = True
        await self._task
        if released:
            for topic in self._filters:
                self.client.message_callback_remove(topic)
        for name, callback in self._installed.items():
            if getattr(self.client, name) is callback:
                setattr(self.client, name, self._old[name])
        if released:
            self.client.manual_ack_set(self._previous_manual_ack)
        self._attached = False
        await self.storage.close()
        return released
