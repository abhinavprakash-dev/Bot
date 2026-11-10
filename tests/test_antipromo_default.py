import unittest
from unittest.mock import patch

from botcore import ui
from botcore.security import rules


class AntipromoDefaultPunishmentTests(unittest.TestCase):
    def test_antipromo_default_punishment_is_timeout(self):
        self.assertEqual(rules.PROTECTIONS["antipromo"].punishment, "timeout")

    def test_antipromo_is_punishable(self):
        self.assertIn("antipromo", rules.PUNISHABLE_MODULES)

    def test_security_skip_alert_uses_animated_brand_icon_without_old_emoji(self):
        with patch.dict(ui.EMBED_ICON_URLS, {"kx_ui_info": "https://cdn.example/globe.gif"}):
            embed = ui.info("Security Alert · Skipped")

        self.assertEqual(embed.title, "✧ Security Alert · Skipped")
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/globe.gif")
        self.assertNotIn("emoji_1", ui.SPIDER_EMOJI)

    def test_existing_animated_title_is_not_restyled_or_replaced(self):
        title = "<a:existing:123456789012345678> Existing alert"

        with patch.dict(ui.EMBED_ICON_URLS, {"kx_ui_warn": "https://cdn.example/shield.gif"}):
            embed = ui.warn(title)

        self.assertEqual(embed.title, title)
        self.assertIsNone(embed.thumbnail.url)


if __name__ == "__main__":
    unittest.main()
