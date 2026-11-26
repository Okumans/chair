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
the example configuration contains no credentials. Broker TLS with certificate
and hostname verification is required except for explicitly enabled loopback
development.

The service starts the MQTT client and the serial adapter, and systemd restarts
it after a reported fatal protocol or connection recovery error. Back up any
existing bridge database before upgrading; the MQTT schema upgrade prevents the
older protocol-only bridge from opening it. Deployment and hardware acceptance
have not yet been performed.
