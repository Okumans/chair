"""MQTT protocol worker for the bridge's own Paho client.

All methods run on a single worker thread. Paho callbacks must be queued to that
worker, so callbacks cannot race the registration of a publish MID.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions

from .contract import ContractError, decode
from .store import UNFINISHED, CommandAdmissionError

if TYPE_CHECKING:
    from paho.mqtt.reasoncodes import ReasonCode

    from .models import JsonObject
    from .store import CommandDisposition, MqttStore


@dataclass
class Flight:
    kind: str
    key: str
    payload: bytes
    deadline: float


@dataclass(frozen=True)
class Oversized:
    """Bounded representation of an invalid MQTT payload."""

    digest: str


_Incoming = tuple[int, int, int, bool, str, bytes | Oversized, float]
"""A queued MQTT message: generation, MID, QoS, retain, topic, payload, time."""


class Coordinator:
    def __init__(
        self,
        store: MqttStore,
        client: mqtt.Client,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        token_is_current: Callable[[int], bool] = lambda _: True,
        on_command: Callable[
            [JsonObject, CommandDisposition], object
        ] = lambda _request, _disposition: None,
    ) -> None:
        self.store: MqttStore = store
        self.client: mqtt.Client = client
        self.monotonic: Callable[[], float] = monotonic
        self.wall_clock: Callable[[], float] = wall_clock
        self.token_is_current: Callable[[int], bool] = token_is_current
        self.on_command: Callable[[JsonObject, CommandDisposition], object] = on_command
        self.connected: bool = False
        self.ready: bool = False
        self.draining: bool = False
        self.fatal: str | None = None
        self.reconnect_required: bool = False
        self.generation: int = 0
        self.subscriptions: set[int | None] = set()
        self.subscription_deadline: float = 0.0
        self.flights: dict[int, Flight] = {}
        self.retry_until: dict[tuple[str, str], float] = {}
        self.pending_incoming: list[_Incoming] = []
        self.first_connection: bool = True
        self.next_cleanup: float = 0.0
        self.storage_ok: bool = True
        self._last_status: (
            tuple[tuple[bool, bool, bool, bool, bool], tuple[int, int, int, int]] | None
        ) = None
        self._last_status_check = 0.0
        with self.store.db:
            self.store.db.execute(
                "UPDATE mqtt_outbox SET state='pending' WHERE state IN ('inflight','waiting')"
            )
            # retry_at_ms holds this worker's monotonic clock; a new worker retries now.
            self.store.db.execute(
                f"UPDATE mqtt_outbox SET retry_at_ms=0 WHERE state IN {UNFINISHED} AND retry_at_ms!=0"
            )

    def _retry_at(self, delay: float) -> int:
        """Return the outbox due time, in monotonic milliseconds, after ``delay``."""
        return int((self.monotonic() + delay) * 1000)

    def on_connect(
        self, flags: mqtt.ConnectFlags, reason: ReasonCode, generation: int
    ) -> None:
        if reason.is_failure:
            # The owner's network loop retries a refused CONNACK; the next
            # successful CONNACK restores normal operation.
            self.connected, self.ready = False, False
            return
        if self.fatal:
            return  # Reconnecting cannot clear it, so leave chair topics unsubscribed.
        if self.first_connection and flags.session_present:
            self.fatal = "new bridge unexpectedly resumed an MQTT session"
            self.reconnect_required = True
            return
        self.first_connection = False
        self.generation = generation
        self.connected, self.ready = True, False
        self.reconnect_required = False
        for flight in self.flights.values():
            flight.deadline = self.monotonic() + 30
        self.subscriptions.clear()
        self.subscription_deadline = self.monotonic() + 30
        with self.store.db:
            projections = self.store.db.execute(
                "SELECT topic,desired FROM mqtt_projection WHERE desired IS NOT NULL"
            ).fetchall()
            for topic, desired in projections:
                self.store.queue_projection(topic, desired, force=True)
        node = next(iter(self.store.mapping))
        for topic in (
            f"smartchair/v1/nodes/{node}/commands/config",
            f"smartchair/v1/bridges/{self.store.bridge_id}/receipts",
        ):
            options = SubscribeOptions(qos=1, retainAsPublished=True, retainHandling=2)
            rc, mid = self.client.subscribe(topic, options=options)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                # Usually a disconnect race; a fresh connection resubscribes.
                self.reconnect_required = True
                return
            self.subscriptions.add(mid)

    def on_disconnect(self) -> None:
        self.connected, self.ready = False, False
        self.pending_incoming.clear()

    def on_subscribe(self, mid: int, reasons: Sequence[ReasonCode]) -> None:
        if self.fatal:
            return
        if mid not in self.subscriptions:
            return  # A SUBACK from an earlier connection.
        if len(reasons) != 1 or reasons[0].is_failure or reasons[0].value != 1:
            self.ready = False
            self.fatal = "MQTT subscription rejected, downgraded or unmatched"
            return
        self.subscriptions.remove(mid)
        self.ready = not self.subscriptions
        if self.ready:
            pending, self.pending_incoming = self.pending_incoming, []
            for message in pending:
                self.on_message(*message[:-1], self.wall_clock())

    def on_message(
        self,
        generation: int,
        mid: int,
        qos: int,
        retain: bool,
        topic: str,
        payload: bytes | Oversized,
        now: float,
    ) -> None:
        if (
            not self.connected
            or self.draining
            or self.fatal
            or self.reconnect_required
            or generation != self.generation
            or not self.token_is_current(generation)
        ):
            return
        if not self.ready:
            if len(self.pending_incoming) >= 64:
                self.reconnect_required = True
            else:
                self.pending_incoming.append(
                    (generation, mid, qos, retain, topic, payload, now)
                )
            return
        node = next(iter(self.store.mapping))
        command_topic = f"smartchair/v1/nodes/{node}/commands/config"
        receipt_topic = f"smartchair/v1/bridges/{self.store.bridge_id}/receipts"
        try:
            if topic == command_topic:
                try:
                    if retain:
                        raise CommandAdmissionError("retained_command")
                    if isinstance(payload, Oversized):
                        raise CommandAdmissionError("invalid_schema")
                    disposition = self.store.admit_command(
                        topic, payload, now=now, qos=qos, retain=retain
                    )
                    self.on_command(decode(payload), disposition)
                except (ContractError, CommandAdmissionError) as error:
                    reason = (
                        error.reason
                        if isinstance(error, CommandAdmissionError)
                        else "invalid_schema"
                    )
                    retryable = (
                        error.retryable
                        if isinstance(error, CommandAdmissionError)
                        else False
                    )
                    command_id: str | None = None
                    if not isinstance(payload, Oversized):
                        try:
                            value = decode(payload)
                            if not isinstance(error, CommandAdmissionError):
                                message = value.get("message")
                                message_object = (
                                    cast("JsonObject", message)
                                    if isinstance(message, dict)
                                    else None
                                )
                                if value.get("schema_version") != 1 or (
                                    message_object is not None
                                    and message_object.get("version") != 2
                                ):
                                    reason = "unsupported_version"
                                elif (
                                    message_object is not None
                                    and message_object.get("node_id") != node
                                ):
                                    reason = "node_mismatch"
                                elif (
                                    message_object is not None
                                    and message_object.get("type") == "CONFIG_SET"
                                ):
                                    from .protocol_bridge import configuration

                                    keys = {
                                        "revision",
                                        "heartbeat_s",
                                        "movement_batch_s",
                                        "movement_enabled",
                                        "radio_windows_enabled",
                                        "radio_interval_s",
                                        "radio_open_s",
                                        "radio_motion_tail_s",
                                    }
                                    if keys.issubset(
                                        message_object
                                    ) and not configuration(
                                        {key: message_object[key] for key in keys}
                                    ):
                                        reason = "invalid_config"
                            # A non-object message has no .get(); the original
                            # AttributeError was swallowed, leaving command_id None.
                            command_message = value.get("message", {})
                            if isinstance(command_message, dict):
                                candidate = cast("JsonObject", command_message).get(
                                    "command_id"
                                )
                                from .protocol_bridge import canonical_uuid

                                command_id = canonical_uuid(candidate)
                        except ContractError:
                            if not isinstance(error, CommandAdmissionError):
                                reason = "invalid_json"
                        except (ValueError, TypeError, AttributeError):
                            pass
                    self.store.reject_command(
                        reason, command_id=command_id, now=now, retryable=retryable
                    )
            elif topic == receipt_topic:
                if isinstance(payload, Oversized):
                    self.store.reject_receipt_digest(
                        topic, payload.digest, "invalid receipt"
                    )
                else:
                    try:
                        self.store.accept_receipt(
                            topic, payload, qos=qos, retain=retain
                        )
                    except (ContractError, ValueError):
                        self.store.reject_receipt(topic, payload, "invalid receipt")
            else:
                self.reconnect_required = True
                return
        except (sqlite3.Error, OSError):
            self.storage_ok = False
            self.reconnect_required = True
            return
        if (
            qos > 0
            and self.token_is_current(generation)
            and self.client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS
        ):
            self.reconnect_required = True

    def _publish(
        self,
        kind: str,
        key: str,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
    ) -> bool:
        properties: Properties | None = None
        if payload:
            properties = Properties(PacketTypes.PUBLISH)
            properties.PayloadFormatIndicator = 1
            properties.ContentType = "application/json"
        info = self.client.publish(
            topic, payload=payload, qos=qos, retain=retain, properties=properties
        )
        # Paho keeps a QoS>0 publish made during a disconnect race and sends it
        # under the same MID after reconnecting, so it is already in flight.
        queued = qos and info.rc == mqtt.MQTT_ERR_NO_CONN
        if info.rc != mqtt.MQTT_ERR_SUCCESS and not queued:
            if qos and info.rc in {mqtt.MQTT_ERR_PAYLOAD_SIZE, mqtt.MQTT_ERR_INVAL}:
                self._publication_failure(
                    Flight(kind, key, payload, 0), 0x95, str(info.rc)
                )
            elif qos and info.rc in {
                mqtt.MQTT_ERR_ACL_DENIED,
                mqtt.MQTT_ERR_AUTH,
                mqtt.MQTT_ERR_NOT_SUPPORTED,
                mqtt.MQTT_ERR_PROTOCOL,
            }:
                self._publication_failure(
                    Flight(kind, key, payload, 0), 0x87, str(info.rc)
                )
            elif qos and info.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
                if kind == "outbox":
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE mqtt_outbox SET retry_at_ms=? WHERE publication_id=?",
                            (self._retry_at(1), key),
                        )
                else:
                    self.retry_until[(kind, key)] = self.monotonic() + 1
            return False
        if qos:
            if info.mid in self.flights:
                self.fatal = "MQTT packet identifier collision"
                self.reconnect_required = True
                return False
            self.flights[info.mid] = Flight(kind, key, payload, self.monotonic() + 30)
        return True

    def _send_outbox(self, now_mono: float) -> None:
        active = {
            flight.key for flight in self.flights.values() if flight.kind == "outbox"
        }
        # New work goes first, then receipt retries; each query reads only due rows
        # through the (state, retry_at_ms) index and at most the free flight slots.
        for state in ("pending", "waiting"):
            capacity = 16 - len(self.flights)
            if capacity <= 0:
                return
            rows = self.store.db.execute(
                """SELECT publication_id,topic,payload FROM mqtt_outbox
                   WHERE state=? AND retry_at_ms<=? ORDER BY retry_at_ms,rowid LIMIT ?""",
                (state, int(now_mono * 1000), capacity + len(active)),
            ).fetchall()
            for row in rows:
                if len(self.flights) >= 16:
                    return
                identity = row["publication_id"]
                if identity in active:
                    continue
                if self._publish(
                    "outbox",
                    identity,
                    row["topic"],
                    row["payload"],
                    qos=1,
                    retain=False,
                ):
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE mqtt_outbox SET state='inflight' WHERE publication_id=? AND state!='completed'",
                            (identity,),
                        )

    def _send_projections(self) -> None:
        active = {
            flight.key
            for flight in self.flights.values()
            if flight.kind == "projection"
        }
        rows = self.store.db.execute("""SELECT p.* FROM mqtt_projection p
            WHERE (p.inflight IS NOT NULL OR p.dirty=1) AND NOT EXISTS
              (SELECT 1 FROM mqtt_quarantine q WHERE q.kind='projection' AND q.item_key=p.topic)
            ORDER BY CASE WHEN length(p.desired)=0 THEN 0 ELSE 1 END,p.topic""").fetchall()
        for row in rows:
            if len(self.flights) >= 16:
                break
            topic = row["topic"]
            if topic in active or self.monotonic() < self.retry_until.get(
                ("projection", topic), 0
            ):
                continue
            payload = row["inflight"] if row["inflight"] is not None else row["desired"]
            if payload is None:
                continue
            if topic.endswith("/status") and b'"type":"COMMAND_STATUS"' in payload:
                command_id = topic.split("/")[-2]
                cmd = self.store.db.execute(
                    "SELECT broker_terminal FROM mqtt_commands WHERE id=?",
                    (command_id,),
                ).fetchone()
                if (
                    cmd
                    and not cmd[0]
                    and json.loads(payload)["status"]
                    in {"applied", "rejected", "expired"}
                ):
                    retained = self.store.db.execute(
                        "SELECT count(*) FROM mqtt_commands WHERE broker_terminal=1"
                    ).fetchone()[0]
                    flying = sum(
                        1
                        for f in self.flights.values()
                        if f.kind == "projection"
                        and f.key.endswith("/status")
                        and f.payload
                        and json.loads(f.payload).get("status")
                        in {"applied", "rejected", "expired"}
                    )
                    if retained + flying >= 256:
                        continue
            if self._publish("projection", topic, topic, payload, qos=1, retain=True):
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE mqtt_projection SET inflight=? WHERE topic=?",
                        (payload, topic),
                    )

    def _send_misc(self) -> None:
        for table, kind, keyname in [
            ("mqtt_errors", "error", "id"),
            ("mqtt_responses", "response", "command_id"),
        ]:
            active = {
                flight.key for flight in self.flights.values() if flight.kind == kind
            }
            for row in self.store.db.execute(
                f"""SELECT t.* FROM {table} t WHERE NOT EXISTS
                    (SELECT 1 FROM mqtt_quarantine q WHERE q.kind=? AND q.item_key=t.{keyname})
                    ORDER BY t.rowid""",
                (kind,),
            ).fetchall():
                if len(self.flights) >= 16:
                    return
                if row[
                    keyname
                ] not in active and self.monotonic() >= self.retry_until.get(
                    (kind, row[keyname]), 0
                ):
                    self._publish(
                        kind,
                        row[keyname],
                        row["topic"],
                        row["payload"],
                        qos=1,
                        retain=False,
                    )

    def bridge_status(
        self,
        instance_id: str,
        *,
        gateway_connected: bool,
        gateway_ready: bool,
        now: float,
        shutting_down: bool = False,
    ) -> None:
        """Stage a retained bridge-status projection on material changes.

        Critical flags publish immediately; backlog counters are rechecked at most
        every 10 seconds, so the worker loop does not count the outbox each pass.
        """
        now_mono = self.monotonic()
        critical = (
            gateway_connected,
            gateway_ready,
            self.storage_ok,
            self.ready,
            shutting_down,
        )
        last = self._last_status
        if (
            last is not None
            and critical == last[0]
            and now_mono - self._last_status_check < 10
        ):
            return
        self._last_status_check = now_mono
        pending, quarantined = self.store.db.execute(
            f"""SELECT count(*),coalesce(sum(state='quarantined'),0)
               FROM mqtt_outbox WHERE state IN {UNFINISHED}"""
        ).fetchone()
        quarantined += self.store.db.execute(
            "SELECT count(*) FROM mqtt_quarantine"
        ).fetchone()[0]
        overdue = self.store.db.execute(
            "SELECT count(*) FROM mqtt_outbox WHERE state='waiting' AND retry_at_ms<=?",
            (int(now_mono * 1000),),
        ).fetchone()[0]
        counters = (pending, overdue, quarantined, self.store.best_effort_dropped)
        if last is not None and critical == last[0] and counters == last[1]:
            return
        value: JsonObject = {
            "schema_version": 1,
            "type": "BRIDGE_STATUS",
            "bridge_id": self.store.bridge_id,
            "instance_id": instance_id,
            "status": "offline" if shutting_down else "online",
            "reason": "shutdown" if shutting_down else None,
            "observed_at_ms": self.store.millis(now),
        }
        if not shutting_down:
            value.update(
                gateway_connected=gateway_connected,
                gateway_ready=gateway_ready,
                storage_ok=self.storage_ok,
                subscriptions_ready=self.ready,
                pending_publications=pending,
                overdue_receipts=overdue,
                quarantined_publications=quarantined,
                mqtt_best_effort_dropped=self.store.best_effort_dropped,
            )
        topic = f"smartchair/v1/bridges/{self.store.bridge_id}/status"
        from .store import compact

        payload = compact(value)
        with self.store.db:
            self.store.queue_projection(topic, payload)
        self._last_status = (critical, counters)

    def _publication_failure(self, flight: Flight, code: int, description: str) -> None:
        with self.store.db:
            if flight.kind == "outbox":
                row = self.store.db.execute(
                    "SELECT state FROM mqtt_outbox WHERE publication_id=?",
                    (flight.key,),
                ).fetchone()
                if row is None or row[0] == "completed":
                    return
            self.store.db.execute(
                """INSERT INTO mqtt_failures(kind,item_key,code,attempts) VALUES (?,?,?,1)
                   ON CONFLICT(kind,item_key,code) DO UPDATE SET attempts=attempts+1""",
                (flight.kind, flight.key, code),
            )
            attempts = self.store.db.execute(
                "SELECT attempts FROM mqtt_failures WHERE kind=? AND item_key=? AND code=?",
                (flight.kind, flight.key, code),
            ).fetchone()[0]
            quarantine = code != 0x97 and (code != 0x91 or attempts > 1)
            backoff = 0.0
            if code == 0x97:
                delay = min(300, 2 ** min(attempts - 1, 8))
                backoff = random.uniform(delay, min(300, delay * 1.2))
            if flight.kind == "outbox":
                self.store.db.execute(
                    "UPDATE mqtt_outbox SET state=?,error=?,retry_at_ms=? WHERE publication_id=? AND state!='completed'",
                    (
                        "quarantined" if quarantine else "pending",
                        description,
                        self._retry_at(backoff),
                        flight.key,
                    ),
                )
            elif quarantine:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO mqtt_quarantine(kind,item_key,reason) VALUES (?,?,?)",
                    (flight.kind, flight.key, description),
                )
            if code == 0x97 and flight.kind != "outbox":
                self.retry_until[(flight.kind, flight.key)] = self.monotonic() + backoff
            if code == 0x91:
                self.reconnect_required = True

    def on_publish(self, mid: int, reason: ReasonCode) -> None:
        flight = self.flights.pop(mid, None)
        if flight is None:
            return
        if reason.is_failure:
            self._publication_failure(flight, reason.value, str(reason))
            return
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM mqtt_failures WHERE kind=? AND item_key=?",
                (flight.kind, flight.key),
            )
            if flight.kind == "outbox":
                row = self.store.db.execute(
                    "SELECT state,attempts FROM mqtt_outbox WHERE publication_id=?",
                    (flight.key,),
                ).fetchone()
                if row and row["state"] != "completed":
                    attempts = row["attempts"] + 1
                    delay = min(300, 15 * (2 ** min(attempts - 1, 5)))
                    self.store.db.execute(
                        "UPDATE mqtt_outbox SET state='waiting',attempts=?,retry_at_ms=? WHERE publication_id=?",
                        (
                            attempts,
                            self._retry_at(
                                random.uniform(delay, min(300, delay * 1.2))
                            ),
                            flight.key,
                        ),
                    )
            elif flight.kind == "projection":
                row = self.store.db.execute(
                    "SELECT desired FROM mqtt_projection WHERE topic=?", (flight.key,)
                ).fetchone()
                if row:
                    same = row["desired"] == flight.payload
                    self.store.db.execute(
                        "UPDATE mqtt_projection SET inflight=NULL,dirty=? WHERE topic=?",
                        (0 if same else 1, flight.key),
                    )
                    if flight.key.endswith("/status"):
                        command_id = flight.key.split("/")[-2]
                        if not flight.payload:
                            self.store.db.execute(
                                "UPDATE mqtt_commands SET retention_state='deleted',broker_terminal=0 WHERE id=? AND retention_state='pending'",
                                (command_id,),
                            )
                            if same:
                                self.store.db.execute(
                                    "UPDATE mqtt_projection SET desired=NULL WHERE topic=?",
                                    (flight.key,),
                                )
                        elif json.loads(flight.payload).get(
                            "type"
                        ) == "COMMAND_STATUS" and json.loads(flight.payload).get(
                            "status"
                        ) in {"applied", "rejected", "expired"}:
                            self.store.db.execute(
                                "UPDATE mqtt_commands SET broker_terminal=1 WHERE id=?",
                                (command_id,),
                            )
            elif flight.kind == "error":
                self.store.db.execute(
                    "DELETE FROM mqtt_errors WHERE id=? AND payload=?",
                    (flight.key, flight.payload),
                )
            elif flight.kind == "response":
                self.store.db.execute(
                    "DELETE FROM mqtt_responses WHERE command_id=? AND payload=?",
                    (flight.key, flight.payload),
                )

    def tick(self, now: float) -> None:
        now_mono = self.monotonic()
        if self.connected and not self.ready and now_mono >= self.subscription_deadline:
            self.reconnect_required = True
        # Deadlines restart on reconnect; a disconnected client cannot be late.
        if self.connected and any(
            now_mono >= flight.deadline for flight in self.flights.values()
        ):
            self.reconnect_required = True
        if (
            not self.connected
            or not self.ready
            or self.fatal
            or self.reconnect_required
        ):
            return
        if now_mono >= self.next_cleanup:
            self.store.schedule_retention(now)
            self.next_cleanup = now_mono + 60
        self._send_misc()
        self._send_projections()
        self._send_outbox(now_mono)
        if self.store.best_effort and len(self.flights) < 16:
            topic, payload = self.store.best_effort[0]
            if self._publish("movement", "", topic, payload, qos=0, retain=False):
                self.store.best_effort.popleft()
