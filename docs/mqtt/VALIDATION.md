# MQTT revision-2 reference validation

This records tests of the contract/reference kit, not deployment of the production
Pi forwarding feature.

- Pi: Python 3.13.5, jsonschema 4.26.0 and Paho MQTT 2.1.0 in a temporary virtual
  environment. All **22 tests passed**, including the isolated Mosquitto test.
- Local workspace: Python 3.14.4 with the same top-level dependency pins. Of 22
  discovered tests, 21 passed and the real-broker test was skipped because the
  Mosquitto executable was unavailable locally.
- The conformance test checks **69 named valid/invalid fixtures** covering all
  public payload families, ESP packet types, flags, identities and field relations.
- Ruff formatting/lint and Python compilation passed for the reference sources.
- The six JSON examples embedded in the specification remained valid. Fifteen
  positive ESP packet examples and the command template were also checked against
  the existing chair protocol's Python validators.

The real-broker test starts its own anonymous loopback Mosquitto process on an
allocated private port, with temporary files and a dedicated backend database.
It exercises event and command-report ingestion, JSON receipt properties, a fresh
backend process using the same broker/client identity, and duplicate receipt
regeneration without duplicate semantic facts. It terminates only its own test
processes. It never uses the installed broker listener or a serial gateway.

Unit checks additionally cover commit-before-ACK visibility, atomic rollback when
receipt storage fails, publication/semantic identity conflicts, old/new node epochs,
pending receipt recovery, stale incoming tokens, SUBACK/PUBACK deadlines, negative
ACK preservation, and unknown status counters represented as null.

The oversized-delivery follow-up passed all **25 tests** locally with the pinned
dependencies and a temporary loopback Mosquitto broker. It checks durable rejection
before native ACK, no ACK on rejection-storage failure, bounded queue/journal data,
deduplication by the full topic and payload, and valid traffic after the oversized
delivery. The Pi was offline during this follow-up; its earlier 22-test result above
applies to the prior reference-kit revision.

The SUBACK and long-topic follow-up passed all **30 tests** locally with the pinned
dependencies and temporary loopback Mosquitto brokers. A QoS-0-only broker causes
the backend to fail closed; the tests also cover bounded UTF-8 topic prefixes,
full-topic deduplication, durable rejection before native ACK, rejection storage
failure, and valid traffic after rejecting a long topic with a small payload. The
Pi was offline during this follow-up, so these changes have not been tested there.

The production Pi outbox, controller submission outbox, reservation accounting,
permanent-error quarantine and node-epoch maintenance barrier remain specified
future implementation work. Their production fault-injection acceptance cases
are listed in MQTT_SPECS.md. The backend example is a reference consumer, not a
complete production application or supervisor. No ESP firmware, RF timing or
power measurement is part of these checks.
