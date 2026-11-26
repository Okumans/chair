"""High-level Smart Chair facade for a caller-owned Paho MQTT client."""

import copy
import hashlib
import logging
import queue
import sqlite3
import ssl
import threading
import time
import uuid
from concurrent.futures import Future

import paho.mqtt.client as mqtt
from protocol_bridge import Bridge
from pydantic import BaseModel

from .contract import MAX_PAYLOAD
from .coordinator import Coordinator, Oversized
from .models import BridgeConfig, BridgeHealth, CommandNotice, ConfigRequest
from .store import MqttStore, compact

LOG = logging.getLogger(__name__)


class ChairMqttBridge:
    """Attach chair protocol handling to an externally owned MQTTv5 client.

    Attach before connecting the client. The application owns creation, network
    loop, broker connection, reconnection, and final disconnection. The facade
    serializes its SQLite and MQTT protocol work on one worker thread and never
    calls the client's connect, disconnect, loop_start, or loop_stop methods.

    The facade enables manual acknowledgement on the shared client. The owner
    must acknowledge any unrelated incoming subscriptions itself and reserve the
    configured chair command and receipt topic callbacks for this facade.
    """

    def __init__(self, client, config: BridgeConfig):
        if not isinstance(config, BridgeConfig):
            raise TypeError("config must be a BridgeConfig")
        # The application explicitly assumes the Pi's UTC clock is trustworthy.
        self.client, self.config, self.clock_trusted = client, config, lambda: True
        self.instance_id = str(uuid.uuid4())
        self._events = queue.Queue(maxsize=128)
        self._notifications = queue.Queue(maxsize=128)
        self._notification_drops = 0
        self._network_lock = threading.Lock()
        self._network_generation = 0
        self._network_connected = False
        self._ever_network_connected = False
        self._overflow = threading.Event()
        self._startup = threading.Event()
        self._startup_error = None
        self._stopping = False
        self._thread = None
        self._old_handlers = {}
        self._installed_handlers = {}
        self._registered_filters = []
        self._previous_manual_ack = None
        self._configured_will = False
        self._gateway_connected = False
        self._gateway_outgoing = None
        self._serial_adapter = None
        self._shutting_down = False
        self._store = self._coordinator = self._gateway = None
        self._attached = False
        self._ever_started = False

    def connect_options(self):
        """Return MQTTv5 connect options for the application's client.connect()."""
        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.SessionExpiryInterval = 86400
        properties.ReceiveMaximum = 16
        properties.MaximumPacketSize = 65536
        return {
            "keepalive": 30,
            "clean_start": mqtt.MQTT_CLEAN_START_FIRST_ONLY,
            "properties": properties,
        }

    def _configure_will(self):
        value = {
            "schema_version": 1,
            "type": "BRIDGE_STATUS",
            "bridge_id": self.config.bridge_id,
            "instance_id": self.instance_id,
            "status": "offline",
            "reason": "connection_lost",
            "observed_at_ms": None,
        }
        properties = mqtt.Properties(mqtt.PacketTypes.WILLMESSAGE)
        properties.WillDelayInterval = 0
        properties.PayloadFormatIndicator = 1
        properties.ContentType = "application/json"
        self.client.will_set(
            f"smartchair/v1/bridges/{self.config.bridge_id}/status",
            payload=compact(value),
            qos=1,
            retain=True,
            properties=properties,
        )

    def _enqueue(self, event):
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self._overflow.set()

    def _current_token(self, token):
        with self._network_lock:
            return self._network_connected and self._network_generation == token

    def _chain(self, name, handler):
        old = getattr(self.client, name)
        self._old_handlers[name] = old

        def wrapper(*args):
            handler(*args)
            if old is not None:
                try:
                    old(*args)
                except Exception:
                    LOG.exception("Application MQTT callback %s failed", name)

        setattr(self.client, name, wrapper)
        self._installed_handlers[name] = wrapper

    def _on_connect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            token = self._network_generation
            self._network_connected = not reason.is_failure
            self._ever_network_connected |= not reason.is_failure
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
                    security_error = (
                        "MQTT TLS must verify the broker certificate and hostname"
                    )
            elif not (
                self.config.allow_local_plaintext
                and getattr(client, "_host", None) in {"localhost", "127.0.0.1", "::1"}
            ):
                security_error = (
                    "MQTT plaintext is permitted only for explicit loopback development"
                )
        self._enqueue(("connect", token, flags, reason, security_error))

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            self._network_connected = False
        self._enqueue(("disconnect",))

    def _on_subscribe(self, client, userdata, mid, reasons, properties):
        self._enqueue(("subscribe", mid, tuple(reasons)))

    def _on_unsubscribe(self, client, userdata, mid, reasons, properties):
        self._enqueue(("unsubscribe", mid, tuple(reasons)))

    def _on_publish(self, client, userdata, mid, reason, properties):
        self._enqueue(("puback", mid, reason))

    def _on_message(self, client, userdata, message):
        with self._network_lock:
            token = self._network_generation
        topic = message.topic
        if len(message.payload) > MAX_PAYLOAD:
            digest = hashlib.sha256()
            digest.update(topic.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(message.payload)
            payload = Oversized(digest.hexdigest())
        else:
            payload = bytes(message.payload)
        self._enqueue(
            (
                "message",
                token,
                message.mid,
                message.qos,
                bool(message.retain),
                topic,
                payload,
                time.time(),
            )
        )

    def _notice(self, request, disposition):
        notice = CommandNotice(
            request=ConfigRequest.model_validate(request), disposition=disposition
        )
        try:
            self._notifications.put_nowait(notice)
        except queue.Full:
            try:
                self._notifications.get_nowait()
            except queue.Empty:
                pass
            self._notification_drops += 1
            self._notifications.put_nowait(notice)

    def attach(self):
        """Start the protocol worker and attach handlers; do not connect the client."""
        if self._ever_started:
            raise RuntimeError(
                "a facade and MQTT client must not be reused after detach"
            )
        if self.client.is_connected():
            raise ValueError("attach before connecting the supplied MQTT client")
        if self.client.callback_api_version != mqtt.CallbackAPIVersion.VERSION2:
            raise ValueError("Paho callback API version 2 is required")
        if getattr(self.client, "_protocol", None) != mqtt.MQTTv5:
            raise ValueError("the supplied client must use MQTT 5")
        if getattr(self.client, "_will", False):
            raise ValueError("the supplied client already has a Last Will")
        self._ever_started = True
        self._thread = threading.Thread(
            target=self._run, name="chair-mqtt-worker", daemon=True
        )
        self._thread.start()
        self._startup.wait()
        if self._startup_error is not None:
            raise RuntimeError(
                "could not start chair MQTT worker"
            ) from self._startup_error
        try:
            self._previous_manual_ack = getattr(self.client, "_manual_ack", None)
            self.client.manual_ack_set(True)
            self._configure_will()
            self._configured_will = True
            self._chain("on_connect", self._on_connect)
            self._chain("on_disconnect", self._on_disconnect)
            self._chain("on_subscribe", self._on_subscribe)
            self._chain("on_unsubscribe", self._on_unsubscribe)
            self._chain("on_publish", self._on_publish)
            for topic in (
                f"smartchair/v1/nodes/{self.config.node_id}/commands/config",
                f"smartchair/v1/bridges/{self.config.bridge_id}/receipts",
            ):
                self.client.message_callback_add(topic, self._on_message)
                self._registered_filters.append(topic)
            self._attached = True
        except Exception:
            self._detach_handlers()
            self._stopping = True
            self._thread.join(timeout=5)
            raise
        return self

    def _detach_handlers(self, *, release_subscriptions=True):
        for name, installed in self._installed_handlers.items():
            if getattr(self.client, name) is installed:
                setattr(self.client, name, self._old_handlers[name])
        if release_subscriptions:
            for topic in self._registered_filters:
                self.client.message_callback_remove(topic)
            self._registered_filters.clear()
            if self._previous_manual_ack is not None:
                self.client.manual_ack_set(self._previous_manual_ack)
                self._previous_manual_ack = None
            if self._configured_will:
                self.client.will_clear()
                self._configured_will = False

    def _call(self, name, *args, timeout=10):
        if not self._attached or self._stopping:
            raise RuntimeError("facade is not attached")
        future = Future()
        self._events.put(("call", future, name, args), timeout=timeout)
        return future.result(timeout=timeout)

    def publish(self, message, *, source_mac):
        """Accept validated sensor data; reliable MQTT work commits before return."""
        if isinstance(message, BaseModel):
            message = message.model_dump(mode="json")
        if not isinstance(message, dict):
            raise TypeError("sensor data must be a Pydantic model or dictionary")
        return self._call("publish", copy.deepcopy(message), source_mac)

    def receive(self, timeout=None):
        """Return the next command notification, or None on timeout.

        Notifications are best effort; durable command dispatch is managed by
        the facade even if the application does not read notifications.
        """
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def gateway_opened(self):
        """Reset gateway state and return the handshake for the serial adapter."""
        return self._call("gateway_opened")

    def gateway_closed(self):
        """Tell the protocol engine its gateway serial connection has closed."""
        self._call("gateway_closed")

    def gateway_received(self, envelope):
        """Feed an envelope from the provisioned gateway to the protocol engine."""
        return self._call("gateway_received", copy.deepcopy(envelope))

    def gateway_next(self):
        """Return the next gateway message, if an ACK or command is ready."""
        return self._call("gateway_next")

    def gateway_write(self, message, port):
        """Write a generated gateway message after its final dispatch guard.

        The port must provide bounded writes; use the supplied serial adapter for
        normal operation. The radio lease and deadline are checked for each write.
        """
        if not callable(getattr(port, "write", None)):
            raise TypeError("port must provide a write method")
        return self._call("gateway_write", copy.deepcopy(message), port)

    def start_gateway(self, port):
        """Start the optional serial adapter without managing the MQTT client."""
        if not self._attached or self._serial_adapter is not None:
            raise RuntimeError(
                "facade must be attached and gateway not already started"
            )
        from .serial_gateway import SerialGateway

        self._serial_adapter = SerialGateway(self, port)
        self._serial_adapter.start()

    def stop_gateway(self):
        """Stop the optional serial adapter and leave the MQTT client running."""
        if self._serial_adapter is not None:
            self._serial_adapter.stop()
            self._serial_adapter = None

    def health(self):
        """Return the bridge's current connection, storage, and publication health."""
        return self._call("health")

    def retry_quarantined(self):
        """Explicitly retry the original bytes after correcting broker policy."""
        return self._call("retry_quarantined")

    def detach(self):
        """Detach callbacks and stop this worker without disconnecting the MQTT client.

        Returns whether an offline status was acknowledged. If false, the owner
        should abort rather than gracefully disconnect so the broker can send the
        Last Will; the facade never takes ownership of connection lifecycle.
        """
        if not self._attached:
            return False
        self.stop_gateway()
        confirmed = False
        release_subscriptions = not self._ever_network_connected
        if self.client.is_connected():
            self._call("shutdown")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self._call("shutdown_confirmed"):
                    confirmed = True
                    break
                time.sleep(0.05)
            self._call("unsubscribe")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self._call("unsubscribed"):
                    release_subscriptions = True
                    break
                if not self.client.is_connected():
                    break
                time.sleep(0.05)
            if not release_subscriptions and self.client.is_connected():
                # Keep the worker and manual ACKs active until the caller aborts.
                return False
        elif self._attached:
            release_subscriptions = release_subscriptions or self._call("unsubscribed")
        self._attached = False
        self._detach_handlers(release_subscriptions=release_subscriptions)
        self._stopping = True
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("chair worker did not stop")
        return confirmed and release_subscriptions

    def _dispatch_call(self, name, args):
        if self._shutting_down and name not in {
            "health",
            "shutdown",
            "shutdown_confirmed",
            "unsubscribe",
            "unsubscribed",
        }:
            raise RuntimeError("facade is shutting down")
        if name == "publish":
            message, source_mac = args
            result = self._gateway.receive(
                {"kind": "telemetry", "source_mac": source_mac, "message": message},
                time.time(),
                time.monotonic(),
            )
            return not result.get("duplicate", False)
        if name == "gateway_opened":
            self._gateway_connected = True
            self._gateway_outgoing = None
            return self._gateway.reset()
        if name == "gateway_closed":
            self._gateway_connected = False
            self._gateway_outgoing = None
            self._gateway.reset()
            return None
        if name == "gateway_received":
            return self._gateway.receive(args[0], time.time(), time.monotonic())
        if name == "gateway_next":
            self._gateway_outgoing = self._gateway.next(time.time(), time.monotonic())
            return copy.deepcopy(self._gateway_outgoing)
        if name == "gateway_write":
            message, port = args
            if message != self._gateway_outgoing or not self._gateway_connected:
                return False
            if message.get("kind") == "command":
                command_id = message["message"]["command_id"]
                pending = self._store.pending(time.time(), monotonic=time.monotonic())
                if not any(row["id"] == command_id for row in pending):
                    self._gateway.radio_pending = None
                    self._gateway_outgoing = None
                    return False
                deadline = self._store._command_deadlines.get(command_id, 0)

                def allowed():
                    return time.monotonic() < deadline

            else:
                allowed = None
            sent = self._gateway.send(port, message, before_write=allowed)
            self._gateway_outgoing = None
            return sent
        if name == "shutdown":
            self._shutting_down = True
            self._coordinator.draining = True
            self._coordinator.bridge_status(
                self.instance_id,
                gateway_connected=self._gateway_connected,
                gateway_ready=self._gateway_connected
                and self._gateway.hello_request is None,
                now=time.time(),
                shutting_down=True,
            )
            return None
        if name == "unsubscribe":
            return self._coordinator.unsubscribe_owned_topics()
        if name == "unsubscribed":
            return self._coordinator.unsubscribed
        if name == "shutdown_confirmed":
            topic = f"smartchair/v1/bridges/{self.config.bridge_id}/status"
            row = self._store.db.execute(
                "SELECT desired,inflight,dirty FROM mqtt_projection WHERE topic=?",
                (topic,),
            ).fetchone()
            return bool(
                row
                and row["inflight"] is None
                and not row["dirty"]
                and row["desired"]
                and b'"status":"offline"' in row["desired"]
            )
        if name == "health":
            count = self._store.db.execute(
                "SELECT count(*) FROM mqtt_outbox WHERE state!='completed'"
            ).fetchone()[0]
            quarantined = (
                self._store.db.execute(
                    "SELECT count(*) FROM mqtt_outbox WHERE state='quarantined'"
                ).fetchone()[0]
                + self._store.db.execute(
                    "SELECT count(*) FROM mqtt_quarantine"
                ).fetchone()[0]
            )
            return BridgeHealth(
                mqtt_connected=self._coordinator.connected,
                subscriptions_ready=self._coordinator.ready,
                gateway_connected=self._gateway_connected,
                gateway_ready=self._gateway_connected
                and self._gateway.hello_request is None,
                storage_ok=self._coordinator.storage_ok,
                clock_trusted=bool(self.clock_trusted()),
                pending_publications=count,
                quarantined_publications=quarantined,
                mqtt_best_effort_dropped=self._store.best_effort_dropped,
                notifications_dropped=self._notification_drops,
                reconnect_required=self._coordinator.reconnect_required,
                fatal=self._coordinator.fatal,
            )
        if name == "retry_quarantined":
            with self._store.db:
                count = self._store.db.execute(
                    "UPDATE mqtt_outbox SET state='pending',error=NULL WHERE state='quarantined'"
                ).rowcount
                count += self._store.db.execute(
                    "SELECT count(*) FROM mqtt_quarantine"
                ).fetchone()[0]
                self._store.db.execute("DELETE FROM mqtt_quarantine")
                self._store.db.execute("DELETE FROM mqtt_failures")
            return count
        raise ValueError("unknown facade operation")

    def _run(self):
        try:
            self._store = MqttStore(
                self.config.database_path,
                bridge_id=self.config.bridge_id,
                consumer_id=self.config.consumer_id,
                mapping={self.config.node_id: self.config.sensor_mac},
                clock_trusted=self.clock_trusted,
                wall_clock=time.time,
            )
            self._gateway = Bridge(
                self._store, {self.config.node_id: self.config.sensor_mac}
            )
            self._coordinator = Coordinator(
                self._store,
                self.client,
                token_is_current=self._current_token,
                on_command=self._notice,
            )
        except Exception as error:
            if self._store is not None:
                self._store.db.close()
            self._startup_error = error
            self._startup.set()
            return
        self._startup.set()
        next_expiry_check = 0.0
        try:
            while not self._stopping:
                if self._overflow.is_set():
                    self._coordinator.reconnect_required = True
                    self._overflow.clear()
                try:
                    item = self._events.get(timeout=0.05)
                except queue.Empty:
                    item = None
                if item is not None:
                    kind, *args = item
                    try:
                        if kind == "call":
                            future, name, call_args = args
                            if not future.cancelled():
                                try:
                                    future.set_result(
                                        self._dispatch_call(name, call_args)
                                    )
                                except Exception as error:
                                    future.set_exception(error)
                        elif kind == "connect":
                            token, flags, reason, security_error = args
                            self._coordinator.on_connect(
                                flags, reason, token, security_error
                            )
                        elif kind == "disconnect":
                            self._coordinator.on_disconnect()
                        elif kind == "subscribe":
                            self._coordinator.on_subscribe(*args)
                        elif kind == "unsubscribe":
                            self._coordinator.on_unsubscribe(*args)
                        elif kind == "puback":
                            self._coordinator.on_publish(*args)
                        elif kind == "message":
                            self._coordinator.on_message(*args[:-1], time.time())
                    except Exception:
                        LOG.exception("Chair MQTT protocol event failed")
                        self._coordinator.reconnect_required = True
                try:
                    now_mono = time.monotonic()
                    if now_mono >= next_expiry_check:
                        self._store.pending(time.time(), monotonic=now_mono)
                        next_expiry_check = now_mono + 0.25
                    if self._coordinator.connected and not self._shutting_down:
                        self._coordinator.bridge_status(
                            self.instance_id,
                            gateway_connected=self._gateway_connected,
                            gateway_ready=self._gateway_connected
                            and self._gateway.hello_request is None,
                            now=time.time(),
                        )
                    self._coordinator.tick(time.time())
                except Exception as error:
                    LOG.exception("Chair MQTT protocol worker failed")
                    if isinstance(error, (sqlite3.Error, OSError)):
                        self._coordinator.storage_ok = False
                    self._coordinator.reconnect_required = True
        finally:
            self._store.db.close()
