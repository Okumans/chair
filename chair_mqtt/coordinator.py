"""MQTT protocol worker using a caller-owned Paho client.

All methods run on a single worker thread. Paho callbacks must be queued to that
worker, so callbacks cannot race the registration of a publish MID.
"""

import json
import random
import sqlite3
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from .contract import ContractError, decode
from .store import CommandAdmissionError


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


class Coordinator:
    def __init__(
        self,
        store,
        client,
        *,
        monotonic=time.monotonic,
        wall_clock=time.time,
        token_is_current=lambda _: True,
        on_command=lambda *_: None,
    ):
        self.store, self.client, self.monotonic = store, client, monotonic
        self.wall_clock = wall_clock
        self.token_is_current, self.on_command = token_is_current, on_command
        self.connected = False
        self.ready = False
        self.draining = False
        self.fatal = None
        self.reconnect_required = False
        self.generation = 0
        self.subscriptions = set()
        self.unsubscribe_mid = None
        self.unsubscribed = False
        self.subscription_deadline = 0.0
        self.flights = {}
        self.wait_until = {}
        self.retry_until = {}
        self.pending_incoming = []
        self.first_connection = True
        self.next_cleanup = 0.0
        self.storage_ok = True
        self._last_status = None
        self._last_status_at = 0.0
        with self.store.db:
            self.store.db.execute(
                "UPDATE mqtt_outbox SET state='pending' WHERE state IN ('inflight','waiting')"
            )

    def on_connect(self, flags, reason, generation, security_error=None):
        if reason.is_failure:
            # The owner's network loop retries a refused CONNACK; the next
            # successful CONNACK restores normal operation.
            self.connected, self.ready = False, False
            return
        if security_error:
            self.fatal = security_error
            return
        if self.fatal:
            return  # Reconnecting cannot clear it, so leave chair topics unsubscribed.
        if self.first_connection and flags.session_present:
            self.fatal = "new facade unexpectedly resumed an MQTT session"
            self.reconnect_required = True
            return
        self.first_connection = False
        self.generation = generation
        self.connected, self.ready = True, False
        self.reconnect_required = False
        self.unsubscribed = False
        self.unsubscribe_mid = None
        for flight in self.flights.values():
            flight.deadline = self.monotonic() + 30
        self.subscriptions.clear()
        self.subscription_deadline = self.monotonic() + 30
        with self.store.db:
            projections = self.store.db.execute(
                "SELECT topic,desired FROM mqtt_projection WHERE desired IS NOT NULL"
            ).fetchall()
            for topic, desired in projections:
                self.store._queue_projection(topic, desired, force=True)
        node = next(iter(self.store.mapping))
        for topic in (
            f"smartchair/v1/nodes/{node}/commands/config",
            f"smartchair/v1/bridges/{self.store.bridge_id}/receipts",
        ):
            options = mqtt.SubscribeOptions(
                qos=1, retainAsPublished=True, retainHandling=2
            )
            rc, mid = self.client.subscribe(topic, options=options)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                # Usually a disconnect race; a fresh connection resubscribes.
                self.reconnect_required = True
                return
            self.subscriptions.add(mid)

    def on_disconnect(self):
        self.connected, self.ready = False, False
        self.unsubscribe_mid = None
        self.pending_incoming.clear()

    def on_subscribe(self, mid, reasons):
        if self.fatal:
            return
        if mid not in self.subscriptions:
            return  # Other subscriptions belong to the caller-owned MQTT client.
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

    def unsubscribe_owned_topics(self):
        """Remove only this facade's subscriptions before releasing its handlers."""
        if self.unsubscribed:
            return True
        if self.unsubscribe_mid is not None:
            return False
        node = next(iter(self.store.mapping))
        result, mid = self.client.unsubscribe(
            [
                f"smartchair/v1/nodes/{node}/commands/config",
                f"smartchair/v1/bridges/{self.store.bridge_id}/receipts",
            ]
        )
        if result != mqtt.MQTT_ERR_SUCCESS:
            return False
        self.unsubscribe_mid = mid
        return False

    def on_unsubscribe(self, mid, reasons):
        if mid != self.unsubscribe_mid:
            return
        self.unsubscribe_mid = None
        self.unsubscribed = len(reasons) == 2 and all(
            not reason.is_failure for reason in reasons
        )

    def on_message(self, generation, mid, qos, retain, topic, payload, now):
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
                    command_id = None
                    if not isinstance(payload, Oversized):
                        try:
                            value = decode(payload)
                            if not isinstance(error, CommandAdmissionError):
                                message = value.get("message")
                                if value.get("schema_version") != 1 or (
                                    isinstance(message, dict)
                                    and message.get("version") != 2
                                ):
                                    reason = "unsupported_version"
                                elif (
                                    isinstance(message, dict)
                                    and message.get("node_id") != node
                                ):
                                    reason = "node_mismatch"
                                elif (
                                    isinstance(message, dict)
                                    and message.get("type") == "CONFIG_SET"
                                ):
                                    from protocol_bridge import configuration

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
                                    if keys.issubset(message) and not configuration(
                                        {key: message[key] for key in keys}
                                    ):
                                        reason = "invalid_config"
                            candidate = value.get("message", {}).get("command_id")
                            from protocol_bridge import canonical_uuid

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

    def _publish(self, kind, key, topic, payload, *, qos, retain):
        properties = None
        if payload:
            properties = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
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
                    self.wait_until[key] = self.monotonic() + 1
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

    def _send_outbox(self, now_mono):
        rows = self.store.db.execute(
            """SELECT publication_id,topic FROM mqtt_outbox
               WHERE state IN ('pending','waiting')
               ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END,rowid"""
        ).fetchall()
        active = {
            flight.key for flight in self.flights.values() if flight.kind == "outbox"
        }
        for row in rows:
            if len(self.flights) >= 16:
                break
            identity = row["publication_id"]
            if identity in active or now_mono < self.wait_until.get(identity, 0):
                continue
            payload = self.store.db.execute(
                "SELECT payload FROM mqtt_outbox WHERE publication_id=?", (identity,)
            ).fetchone()[0]
            if self._publish(
                "outbox", identity, row["topic"], payload, qos=1, retain=False
            ):
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE mqtt_outbox SET state='inflight' WHERE publication_id=? AND state!='completed'",
                        (identity,),
                    )

    def _send_projections(self):
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

    def _send_misc(self):
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
        self, instance_id, *, gateway_connected, gateway_ready, now, shutting_down=False
    ):
        """Stage a retained bridge-status projection on material changes."""
        now_mono = self.monotonic()
        pending, quarantined = self.store.db.execute(
            """SELECT count(*),coalesce(sum(state='quarantined'),0)
               FROM mqtt_outbox WHERE state!='completed'"""
        ).fetchone()
        quarantined += self.store.db.execute(
            "SELECT count(*) FROM mqtt_quarantine"
        ).fetchone()[0]
        waiting = self.store.db.execute(
            "SELECT publication_id FROM mqtt_outbox WHERE state='waiting'"
        ).fetchall()
        overdue = sum(
            1
            for row in waiting
            if now_mono >= self.wait_until.get(row[0], float("inf"))
        )
        critical = (
            gateway_connected,
            gateway_ready,
            self.storage_ok,
            self.ready,
            shutting_down,
        )
        counters = (pending, overdue, quarantined, self.store.best_effort_dropped)
        if self._last_status is not None:
            old_critical, old_counters = self._last_status
            if critical == old_critical and (
                counters == old_counters or now_mono - self._last_status_at < 10
            ):
                return
        value = {
            "schema_version": 1,
            "type": "BRIDGE_STATUS",
            "bridge_id": self.store.bridge_id,
            "instance_id": instance_id,
            "status": "offline" if shutting_down else "online",
            "reason": "shutdown" if shutting_down else None,
            "observed_at_ms": self.store._millis(now),
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
            self.store._queue_projection(topic, payload)
        self._last_status = (critical, counters)
        self._last_status_at = now_mono

    def _publication_failure(self, flight, code, description):
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
            if flight.kind == "outbox":
                self.store.db.execute(
                    "UPDATE mqtt_outbox SET state=?,error=? WHERE publication_id=? AND state!='completed'",
                    (
                        "quarantined" if quarantine else "pending",
                        description,
                        flight.key,
                    ),
                )
            elif quarantine:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO mqtt_quarantine(kind,item_key,reason) VALUES (?,?,?)",
                    (flight.kind, flight.key, description),
                )
            if code == 0x97:
                delay = min(300, 2 ** min(attempts - 1, 8))
                deadline = self.monotonic() + random.uniform(
                    delay, min(300, delay * 1.2)
                )
                if flight.kind == "outbox":
                    self.wait_until[flight.key] = deadline
                else:
                    self.retry_until[(flight.kind, flight.key)] = deadline
            if code == 0x91:
                self.reconnect_required = True

    def on_publish(self, mid, reason):
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
                    self.store.db.execute(
                        "UPDATE mqtt_outbox SET state='waiting',attempts=? WHERE publication_id=?",
                        (attempts, flight.key),
                    )
                    delay = min(300, 15 * (2 ** min(attempts - 1, 5)))
                    self.wait_until[flight.key] = self.monotonic() + random.uniform(
                        delay, min(300, delay * 1.2)
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

    def tick(self, now):
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
