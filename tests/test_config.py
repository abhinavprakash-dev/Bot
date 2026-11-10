import os
import unittest
from unittest.mock import patch

from botcore.config import _env_bool, _env_ids


class EnvironmentBooleanTests(unittest.TestCase):
    def test_missing_or_empty_value_uses_default(self):
        for value in (None, ""):
            with self.subTest(value=value):
                with patch.dict(os.environ, {}, clear=True):
                    if value is not None:
                        os.environ["MUSIC_ENABLED"] = value
                    self.assertTrue(_env_bool("MUSIC_ENABLED", True))

    def test_false_values_disable_music(self):
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"MUSIC_ENABLED": value}, clear=True):
                    self.assertFalse(_env_bool("MUSIC_ENABLED", True))

    def test_true_values_enable_music(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"MUSIC_ENABLED": value}, clear=True):
                    self.assertTrue(_env_bool("MUSIC_ENABLED", False))

    def test_invalid_value_is_rejected(self):
        with patch.dict(os.environ, {"MUSIC_ENABLED": "sometimes"}, clear=True):
            with self.assertRaisesRegex(SystemExit, "MUSIC_ENABLED must be true or false"):
                _env_bool("MUSIC_ENABLED", True)

    def test_trusted_bot_ids_are_empty_by_default_and_deduplicated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_ids("TRUSTED_ROLE_MANAGEMENT_BOT_IDS"), ())
        with patch.dict(
            os.environ,
            {"TRUSTED_ROLE_MANAGEMENT_BOT_IDS": " 12345,67890,12345 "},
            clear=True,
        ):
            self.assertEqual(
                _env_ids("TRUSTED_ROLE_MANAGEMENT_BOT_IDS"), (12345, 67890)
            )

    def test_trusted_bot_ids_reject_non_numeric_values(self):
        with patch.dict(
            os.environ,
            {"TRUSTED_ROLE_MANAGEMENT_BOT_IDS": "12345,dino"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                SystemExit, "comma-separated list of numeric Discord IDs"
            ):
                _env_ids("TRUSTED_ROLE_MANAGEMENT_BOT_IDS")
