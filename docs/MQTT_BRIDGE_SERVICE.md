# Pi bridge service

The standalone bridge reads a TOML file such as `deploy/bridge.example.toml`.
Create the `chair` service account with access to the gateway serial device,
install the package into `/opt/chair-mqtt`, and place the configuration and
broker CA under `/etc/chair-mqtt`. The systemd unit creates
`/var/lib/chair-mqtt` for the durable database. Adjust the account, paths and
serial permissions for the installation before enabling the unit.

If the broker requires credentials, set `CHAIR_MQTT_USERNAME` and
`CHAIR_MQTT_PASSWORD` in `/etc/chair-mqtt/credentials` and restrict that file to
the service administrator. This file is read by systemd's `EnvironmentFile`;
the example configuration contains no credentials, and the service refuses a
`username` or `password` in the `[mqtt]` table. The `[mqtt]` keys are those of
`BrokerConfig`: `host`, `port` (default 8883), `client_id`, an optional
`ca_file` and `plaintext`. TLS always verifies the broker certificate and host name, against
`ca_file` or else the system trust store, before anything is sent. Only for
development against a broker on the same machine, `plaintext = true` disables
TLS; it is rejected for any host other than `localhost`, `127.0.0.1` or `::1`.
It replaces the former `allow_local_plaintext` key under `[bridge]`, which is now
rejected as unknown.

The service opens the gateway serial port immediately and connects to the broker in
the background, so sensor facts are acknowledged and stored durably while the
broker or network is unavailable; they are published once it is reachable.
The bridge reconnects with a 1–30 second exponential backoff (without the
jitter the MQTT specification asks for), and when it needs MQTT
redelivery it drops the connection itself, at most once every 30 seconds.
Systemd restarts the service only after a fatal protocol error or a storage
failure, which a reconnect cannot fix. On stop the service publishes an offline
status and disconnects once the broker acknowledges it, or aborts the
connection so the broker publishes the Will. A restarted service starts a clean
broker session and republishes its durable publications. Back up any
existing bridge database before upgrading; the MQTT schema upgrade prevents the
older protocol-only bridge from opening it. Deployment and hardware acceptance
have not yet been performed.
