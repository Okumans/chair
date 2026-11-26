"""Paho MQTT 5 clients that the library creates, connects and owns."""

import socket
import ssl

import paho.mqtt.client as mqtt

SESSION_EXPIRY_S = 86400
KEEPALIVE_S = 30
RECEIVE_MAXIMUM = 16
MAXIMUM_PACKET_SIZE = 65536
# The spec's reconnect delay range; Paho doubles the delay but adds no jitter.
RECONNECT_DELAY_S = (1, 30)


def create_client(broker):
    """Return a disconnected client with manual ACKs and verified TLS configured.

    ``ssl.create_default_context`` requires a trusted certificate and a matching
    host name, so verification fails during the TLS handshake, before CONNECT
    or credentials are sent.
    """
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=broker.client_id,
        protocol=mqtt.MQTTv5,
        manual_ack=True,
    )
    if not broker.plaintext:
        cafile = None if broker.ca_file is None else str(broker.ca_file)
        client.tls_set_context(ssl.create_default_context(cafile=cafile))
    if broker.username is not None:
        password = broker.password
        client.username_pw_set(
            broker.username, None if password is None else password.get_secret_value()
        )
    client.reconnect_delay_set(*RECONNECT_DELAY_S)
    return client


def connect(client, broker):
    """Connect in Paho's network thread; it retries until the broker is reachable."""
    properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
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


def abort(client):
    """Drop the connection without DISCONNECT, so the broker keeps the session.

    The broker redelivers unacknowledged messages and publishes any Will; Paho's
    network loop reconnects unless it is being stopped.
    """
    sock = client.socket()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def close(client, *, graceful):
    """Disconnect (or abort) and wait for Paho's network thread to finish."""
    if graceful:
        client.disconnect()
    else:
        abort(client)
    client.loop_stop()
