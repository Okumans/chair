import unittest

from chair_mqtt.console import (
    DEFAULTS,
    build_configuration,
    build_parser,
    parse_assignments,
    settings_from_args,
)


class AssignmentTests(unittest.TestCase):
    def test_parses_integers_and_booleans(self):
        self.assertEqual(
            parse_assignments("radio_interval_s=60 movement_enabled=false"),
            {"radio_interval_s": 60, "movement_enabled": False},
        )

    def test_rejects_unknown_key_missing_equals_and_bad_boolean(self):
        for text in (
            "colour=1",
            "radio_interval_s",
            "movement_enabled=False",
            "heartbeat_s=x",
            "movement_enabled=1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_assignments(text)

    def test_revision_follows_the_known_revision(self):
        config, expires, guessed = build_configuration(
            DEFAULTS, 4, {"radio_interval_s": 60}
        )
        self.assertEqual(
            (config.revision, config.radio_interval_s, expires, guessed),
            (5, 60, 120, False),
        )
        self.assertEqual(config.heartbeat_s, 900)

    def test_revision_is_guessed_without_a_known_revision(self):
        config, _, guessed = build_configuration(DEFAULTS, None, {})
        self.assertEqual((config.revision, guessed), (1, True))

    def test_explicit_revision_and_expiry_win(self):
        config, expires, guessed = build_configuration(
            DEFAULTS, 4, {"revision": 9, "expires": 30}
        )
        self.assertEqual((config.revision, expires, guessed), (9, 30, False))

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError):
            build_configuration(DEFAULTS, 0, {"radio_open_s": 60})


class ArgumentTests(unittest.TestCase):
    def parse(self, *argv, host="localhost"):
        return build_parser().parse_args(
            ["--host", host, "--bridge", "pi-01", "--consumer", "backend-01", *argv]
        )

    def test_plaintext_defaults_to_1883_and_tls_to_8883(self):
        broker, _ = settings_from_args(self.parse("--plaintext"), {})
        self.assertEqual((broker.port, broker.plaintext), (1883, True))
        broker, _ = settings_from_args(self.parse(), {})
        self.assertEqual((broker.port, broker.plaintext), (8883, False))

    def test_explicit_port_wins(self):
        broker, _ = settings_from_args(self.parse("--plaintext", "--port", "1999"), {})
        self.assertEqual(broker.port, 1999)

    def test_client_id_is_generated_per_run(self):
        first, _ = settings_from_args(self.parse(), {})
        second, _ = settings_from_args(self.parse(), {})
        self.assertRegex(first.client_id, r"^chair-console-[0-9a-f]{8}$")
        self.assertNotEqual(first.client_id, second.client_id)

    def test_identities_and_credentials(self):
        broker, client = settings_from_args(
            self.parse("--node", "chair-02"),
            {"CHAIR_MQTT_USERNAME": "tester", "CHAIR_MQTT_PASSWORD": "secret"},
        )
        self.assertEqual(
            (client.node_id, client.bridge_id, client.consumer_id),
            ("chair-02", "pi-01", "backend-01"),
        )
        self.assertEqual(
            (broker.username, broker.password.get_secret_value()),
            ("tester", "secret"),
        )

    def test_empty_credentials_mean_none(self):
        broker, _ = settings_from_args(
            self.parse(), {"CHAIR_MQTT_USERNAME": "", "CHAIR_MQTT_PASSWORD": ""}
        )
        self.assertEqual((broker.username, broker.password), (None, None))

    def test_remote_plaintext_is_refused(self):
        with self.assertRaises(ValueError):
            settings_from_args(self.parse("--plaintext", host="broker.example"), {})
