import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from cogs.security import SecurityCog


class ProtectionToggleEmbedTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, name="KRYO Test Server", icon=None)
        self.state = {"modules": {}, "punishments": {}}
        self.cog = SecurityCog.__new__(SecurityCog)
        self.cog.has_tier = lambda guild_id, tier: True
        self.cog.cfg = lambda guild_id: self.state
        self.cog.guild_settings = lambda guild_id: {"security": self.state}
        self.cog.role_protection = lambda guild_id: {"enabled": True}
        self.cog.sync_master_flag = Mock()
        self.cog.save = Mock()
        self.cog.permission_warnings = lambda guild: ["Missing Manage Messages"]
        self.cog.punishment = lambda guild_id, module: "ban"

    @staticmethod
    def fields(embed):
        return {field.name: field.value for field in embed.fields}

    def test_enable_confirmation_has_status_response_and_setup_warning(self):
        embed = self.cog.toggle_module(self.guild, "antimention", True)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "🟢  `ACTIVE`")
        self.assertEqual(fields["SERVER"], "KRYO Test Server")
        self.assertEqual(fields["AUTOMATED RESPONSE"], "Ban")
        self.assertIn("Missing Manage Messages", fields["SETUP CHECK"])
        self.assertNotIn("Deletes server invites", embed.description)
        self.assertEqual(embed.description, "Monitoring is active for new activity.")
        self.assertIn("SECURITY CONTROL", embed.author.name)
        self.assertTrue(self.state["modules"]["antimention"])

    def test_disable_confirmation_shows_inactive_state(self):
        embed = self.cog.toggle_module(self.guild, "antimention", False)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "⚪  `INACTIVE`")
        self.assertEqual(fields["RESPONSE"], "Instant")
    def test_set_all_uses_the_same_control_center_card(self):
        embed = self.cog.set_all(self.guild, True)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "🟢  `ACTIVE`")
        self.assertEqual(
            fields["AUTOMATED RESPONSE"],
            "Configured per protection in `/antinuke panel`.",
        )
        self.assertTrue(all(self.state["modules"].values()))


if __name__ == "__main__":
    unittest.main()
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.security import (
    ANTINUKE_BANNER_FILENAME,
    PROTECTIONS_BANNER_FILENAME,
    PROTECTIONS_PREVIEW_FILENAME,
    SecurityCog,
)


class ProtectionToggleEmbedTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, name="KRYO Test Server", icon=None)
        self.state = {"modules": {}, "punishments": {}}
        self.cog = SecurityCog.__new__(SecurityCog)
        self.cog.has_tier = lambda guild_id, tier: True
        self.cog.cfg = lambda guild_id: self.state
        self.cog.guild_settings = lambda guild_id: {"security": self.state}
        self.cog.role_protection = lambda guild_id: {"enabled": True}
        self.cog.sync_master_flag = Mock()
        self.cog.save = Mock()
        self.cog.permission_warnings = lambda guild: ["Missing Manage Messages"]
        self.cog.punishment = lambda guild_id, module: "ban"

    @staticmethod
    def fields(embed):
        return {field.name: field.value for field in embed.fields}

    def test_enable_confirmation_has_status_response_and_setup_warning(self):
        embed = self.cog.toggle_module(self.guild, "antimention", True)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "🟢  `ACTIVE`")
        self.assertEqual(fields["SERVER"], "KRYO Test Server")
        self.assertEqual(fields["AUTOMATED RESPONSE"], "Ban")
        self.assertIn("Missing Manage Messages", fields["SETUP CHECK"])
        self.assertNotIn("Deletes server invites", embed.description)
        self.assertEqual(embed.description, "Monitoring is active for new activity.")
        self.assertIn("SECURITY CONTROL", embed.author.name)
        self.assertTrue(self.state["modules"]["antimention"])

    def test_disable_confirmation_shows_inactive_state(self):
        embed = self.cog.toggle_module(self.guild, "antimention", False)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "⚪  `INACTIVE`")
        self.assertNotIn("RESPONSE", fields)
    def test_set_all_uses_the_same_control_center_card(self):
        embed = self.cog.set_all(self.guild, True)
        fields = self.fields(embed)

        self.assertEqual(fields["STATUS"], "🟢  `ACTIVE`")
        self.assertEqual(fields["RESPONSE"], "Instant")
        self.assertTrue(all(self.state["modules"].values()))


class ProtectionBannerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, name="KRYO Test Server", icon=None)
        self.state = {"modules": {}, "punishments": {}}
        self.cog = SecurityCog.__new__(SecurityCog)
        self.cog.has_tier = lambda guild_id, tier: True
        self.cog.cfg = lambda guild_id: self.state
        self.cog.guild_settings = lambda guild_id: {"security": self.state}
        self.cog.role_protection = lambda guild_id: {"enabled": True}
        self.cog.sync_master_flag = Mock()
        self.cog.save = Mock()
        self.cog.permission_warnings = lambda guild: []
        self.cog._enable_assets_ready = lambda: False

    async def test_prefix_enable_edits_activation_banner_to_protections_banner(self):
        message = SimpleNamespace(edit=AsyncMock())
        sent = {}

        async def send(**kwargs):
            sent["embed"] = kwargs["embed"].to_dict()
            sent["file"] = kwargs["file"]
            return message

        ctx = SimpleNamespace(send=AsyncMock(side_effect=send), guild=self.guild)

        with patch("cogs.security.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.cog._send_all_enabled_prefix(ctx, self.cog.set_all(self.guild, True))

        self.assertEqual(sent["file"].filename, ANTINUKE_BANNER_FILENAME)
        sleep.assert_awaited_once()

        edit = message.edit.await_args.kwargs
        self.assertEqual(
            edit["embed"].image.url,
            f"attachment://{PROTECTIONS_BANNER_FILENAME}",
        )
        self.assertEqual(
            edit["embed"].thumbnail.url,
            f"attachment://{PROTECTIONS_PREVIEW_FILENAME}",
        )
        self.assertEqual(
            [file.filename for file in edit["attachments"]],
            [PROTECTIONS_BANNER_FILENAME, PROTECTIONS_PREVIEW_FILENAME],
        )
        for file in edit["attachments"]:
            file.close()
        sent["file"].close()

    async def test_slash_enable_sends_then_edits_original_response(self):
        sent = {}

        async def send_message(**kwargs):
            if "embed" in kwargs:
                sent["embed"] = kwargs["embed"].to_dict()
            sent["file"] = kwargs["file"]

        interaction = SimpleNamespace(
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)),
            edit_original_response=AsyncMock(),
        )

        with patch("cogs.security.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.cog.slash_toggle_all(interaction, True)

        self.assertEqual(sent["file"].filename, ANTINUKE_BANNER_FILENAME)
        sleep.assert_awaited_once()
        edit = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(
            edit["embed"].image.url,
            f"attachment://{PROTECTIONS_BANNER_FILENAME}",
        )
        self.assertEqual(
            [file.filename for file in edit["attachments"]],
            [PROTECTIONS_BANNER_FILENAME, PROTECTIONS_PREVIEW_FILENAME],
        )
        for file in edit["attachments"]:
            file.close()
        sent["file"].close()


if __name__ == "__main__":
    unittest.main()
