import unittest

from chair_mqtt.console import DEFAULTS, build_configuration, parse_assignments


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
