"""Pi-side Smart Chair bridge that owns its MQTT connection."""

import copy
import hashlib
import logging
import queue
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future

import paho.mqtt.client as mqtt
from pydantic import BaseModel

from . import connection
from .contract import MAX_PAYLOAD
from .coordinator import Coordinator, Oversized
from .gateway import GatewayLink
from .models import (
    BridgeConfig,
    BridgeHealth,
    BrokerConfig,
    CommandNotice,
    ConfigRequest,
    PublishResult,
)
from .protocol_bridge import Bridge
from .store import UNFINISHED, MqttStore, compact

LOG = logging.getLogger(__name__)


class ChairBridge:
    """Run the Pi bridge between the gateway and an MQTT 5 broker it connects to.

    ``start`` (or ``with``) opens the durable database, connects in the
    background and, when ``gateway`` is a ``GatewayTransport`` such as
    ``SerialTransport``, starts driving the gateway. The bridge reconnects to
    the broker with backoff and recovers unacknowledged deliveries itself, so a
    broker outage never stops sensor acknowledgements. SQLite and MQTT protocol
    work is serialized on one worker thread.
    """

    def __init__(self, config: BridgeConfig, broker: BrokerConfig, *, gateway=None):
        if not isinstance(config, BridgeConfig):
            raise TypeError("config must be a BridgeConfig")
        if not isinstance(broker, BrokerConfig):
            raise TypeError("broker must be a BrokerConfig")
        if gateway is not None and not callable(getattr(gateway, "open", None)):
            raise TypeError("gateway must be a GatewayTransport")
        # The application explicitly assumes the Pi's UTC clock is trustworthy.
        self.config, self.broker, self.clock_trusted = config, broker, lambda: True
        self._transport, self._link = gateway, None
        self.instance_id = str(uuid.uuid4())
        self._events = queue.Queue(maxsize=128)
        self._notifications = queue.Queue(maxsize=128)
        self._notification_drops = 0
        self._network_lock = threading.Lock()
        self._network_generation = 0
        self._network_connected = False
        self._overflow = threading.Event()
        self._startup = threading.Event()
        self._startup_error = None
        self._stopping = False
        self._thread = None
        self._gateway_connected = False
        self._gateway_outgoing = None
        self._shutting_down = False
        self._store = self._coordinator = self._gateway = None
        self._attached = False
        self._ever_started = False
        self._client = connection.create_client(broker)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_publish = self._on_publish
        self._client.on_message = self._on_message

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
        self._client.will_set(
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

    def _on_connect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            token = self._network_generation
            self._network_connected = not reason.is_failure
        self._enqueue(("connect", token, flags, reason))

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self._network_lock:
            self._network_generation += 1
            self._network_connected = False
        self._enqueue(("disconnect",))

    def _on_subscribe(self, client, userdata, mid, reasons, properties):
        self._enqueue(("subscribe", mid, tuple(reasons)))

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

    def start(self):
        """Open the database, start the worker and connect in the background.

        Returns this bridge without waiting for the broker; check ``health()``.
        A bridge can be started only once.
        """
        if self._ever_started:
            raise RuntimeError("a bridge can be started only once")
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
        self._attached = True
        try:
            self._configure_will()  # Before CONNECT, as the spec requires.
            connection.connect(self._client, self.broker)
            if self._transport is not None:
                self._link = GatewayLink(self, self._transport)
                self._link.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    def _call(self, name, *args, timeout=10):
        if not self._attached or self._stopping:
            raise RuntimeError("bridge is not started")
        future = Future()
        self._events.put(("call", future, name, args), timeout=timeout)
        return future.result(timeout=timeout)

    def publish(self, message, *, source_mac):
        """Accept sensor data that arrived without going through the gateway.

        Reliable MQTT work commits before return. Returns a ``PublishResult``;
        deliver its ``ack`` to the sensor, because data passed here is never
        acknowledged over the gateway radio.
        """
        if isinstance(message, BaseModel):
            message = message.model_dump(mode="json")
        if not isinstance(message, dict):
            raise TypeError("sensor data must be a Pydantic model or dictionary")
        return self._call("publish", copy.deepcopy(message), source_mac)

    def receive(self, timeout=None):
        """Return the next command notification, or None on timeout.

        Notifications are best effort; durable command dispatch is managed by
        the bridge even if the application does not read notifications.
        """
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    # Gateway operations, run on the worker for the GatewayLink thread.

    def _gateway_opened(self):
        return self._call("gateway_opened")

    def _gateway_closed(self):
        self._call("gateway_closed")

    def _gateway_received(self, envelope):
        return self._call("gateway_received", envelope)

    def _gateway_next(self):
        return self._call("gateway_next")

    def _gateway_write(self, message, stream):
        # The radio lease and command deadline are checked before each write.
        return self._call("gateway_write", message, stream)

    def health(self):
        """Return the bridge's current connection, storage, and publication health."""
        return self._call("health")

    def retry_quarantined(self):
        """Explicitly retry the original bytes after correcting broker policy."""
        return self._call("retry_quarantined")

    def stop(self):
        """Stop the gateway and disconnect; return whether offline was confirmed.

        Publishes an offline status and waits up to two seconds for its PUBACK
        before a normal DISCONNECT. Without that confirmation the connection is
        aborted instead, so the broker publishes the bridge's Will.
        """
        if not self._attached:
            return False
        try:
            if self._link is not None:
                self._link.stop()
        finally:
            confirmed = False
            if self._client.is_connected():
                self._call("shutdown")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if self._call("shutdown_confirmed"):
                        confirmed = True
                        break
                    time.sleep(0.05)
            try:
                connection.close(self._client, graceful=confirmed)
            finally:
                self._attached = False
                self._stopping = True
                self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("chair worker did not stop")
        return confirmed

    def _dispatch_call(self, name, args):
        if self._shutting_down and name not in {
            "health",
            "shutdown",
            "shutdown_confirmed",
        }:
            raise RuntimeError("bridge is shutting down")
        if name == "publish":
            message, source_mac = args
            result = self._gateway.receive(
                {"kind": "telemetry", "source_mac": source_mac, "message": message},
                time.time(),
                time.monotonic(),
            )
            if result["kind"] == "ignored":
                return PublishResult(status="ignored", reason=result["reason"])
            accepted = result["message"]
            # Hand the ACK to the caller instead of leaving it for the radio.
            ack = self._gateway.acks.pop(
                (accepted["node_id"], accepted["boot_id"], accepted["seq"]), None
            )
            return PublishResult(
                status="duplicate" if result["duplicate"] else "accepted", ack=ack
            )
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
                f"SELECT count(*) FROM mqtt_outbox WHERE state IN {UNFINISHED}"
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
        raise ValueError("unknown bridge operation")

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
                self._client,
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
        next_expiry_check = next_recovery = 0.0
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
                            token, flags, reason = args
                            self._coordinator.on_connect(flags, reason, token)
                        elif kind == "disconnect":
                            self._coordinator.on_disconnect()
                        elif kind == "subscribe":
                            self._coordinator.on_subscribe(*args)
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
                    coordinator = self._coordinator
                    if (
                        coordinator.reconnect_required
                        and coordinator.connected
                        and not coordinator.fatal
                        and not self._shutting_down
                        and now_mono >= next_recovery
                    ):
                        # Drop the connection so the broker redelivers; Paho reconnects.
                        LOG.warning("Reconnecting MQTT to recover deliveries")
                        next_recovery = now_mono + connection.RECOVERY_INTERVAL_S
                        connection.abort(self._client)
                except Exception as error:
                    LOG.exception("Chair MQTT protocol worker failed")
                    if isinstance(error, (sqlite3.Error, OSError)):
                        self._coordinator.storage_ok = False
                    self._coordinator.reconnect_required = True
        finally:
            self._store.db.close()
