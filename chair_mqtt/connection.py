"""Paho MQTT 5 clients that the library creates, connects and owns."""

import socket
import ssl
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

if TYPE_CHECKING:
    from .models import BrokerConfig

SESSION_EXPIRY_S = 86400
KEEPALIVE_S = 30
RECEIVE_MAXIMUM = 16
MAXIMUM_PACKET_SIZE = 65536
# The spec's reconnect delay range; Paho doubles the delay but adds no jitter.
RECONNECT_DELAY_S = (1, 30)
# While deliveries need redelivery, drop the connection at most this often.
RECOVERY_INTERVAL_S = 30.0


def create_client(broker: "BrokerConfig") -> mqtt.Client:
    """Return a disconnected client with manual ACKs and verified TLS configured.

    ``ssl.create_default_context`` requires a trusted certificate and a matching
    host name, so verification fails during the TLS handshake, before CONNECT
    or credentials are sent.
    """
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=broker.client_id,
        protocol=mqtt.MQTTv5,
        manual_ack=True,
    )
    if not broker.plaintext:
        cafile = None if broker.ca_file is None else str(broker.ca_file)
        client.tls_set_context(ssl.create_default_context(cafile=cafile))  # pyright: ignore[reportUnknownMemberType]  # paho's optional ssl import
    if broker.username is not None:
        password = broker.password
        client.username_pw_set(
            broker.username, None if password is None else password.get_secret_value()
        )
    client.reconnect_delay_set(*RECONNECT_DELAY_S)
    return client


def connect(client: mqtt.Client, broker: "BrokerConfig") -> None:
    """Connect in Paho's network thread; it retries until the broker is reachable."""
    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = SESSION_EXPIRY_S
    properties.ReceiveMaximum = RECEIVE_MAXIMUM
    properties.MaximumPacketSize = MAXIMUM_PACKET_SIZE
    client.connect_async(
        broker.host,
        broker.port,
        keepalive=KEEPALIVE_S,
        clean_start=mqtt.MQTT_CLEAN_START_FIRST_ONLY,
        properties=properties,
    )
    client.loop_start()


def abort(client: mqtt.Client) -> None:
    """Drop the connection without DISCONNECT, so the broker keeps the session.

    The broker redelivers unacknowledged messages and publishes any Will; Paho's
    network loop reconnects unless it is being stopped.
    """
    sock = client.socket()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]  # paho types its TCP/TLS socket as a minimal protocol
        except OSError:
            pass


def close(client: mqtt.Client, *, graceful: bool) -> None:
    """Disconnect (or abort) and wait for Paho's network thread to finish."""
    if graceful:
        client.disconnect()
    else:
        abort(client)
    client.loop_stop()
