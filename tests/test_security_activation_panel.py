import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.security import (
    ANTINUKE_BANNER_FILENAME,
    ANTINUKE_BANNER_FILENAME,
    PROTECTIONS_BANNER_FILENAME,
    PROTECTIONS_PREVIEW_FILENAME,
    AllProtectionsView,
    MODULES,
    SecurityCog,
)


class SecurityActivationPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=123, name="KRYO Test Server", icon=None)
        self.state = {"modules": {}, "punishments": {}}
        self.cog = SecurityCog.__new__(SecurityCog)
        self.cog.has_tier = lambda _guild_id, _tier: True
        self.cog._enable_assets_ready = lambda: False
        self.cog.cfg = lambda _guild_id: self.state
        self.cog.sync_master_flag = Mock()
        self.cog.save = Mock()
        self.cog.permission_warnings = lambda _guild: []
        self.cog.role_protection = lambda _guild_id: {"enabled": True}

    def test_all_enabled_embed_matches_the_new_panel_layout(self):
        embed = self.cog.set_all(self.guild, True)
        view = AllProtectionsView(self.cog, self.guild)

        self.assertTrue(embed.title.endswith("All Protections Enabled"))
        self.assertEqual(embed.author.name, "KRYO  •  SECURITY CONTROL")
        self.assertEqual(
            [field.name for field in embed.fields[:4]],
            ["STATUS", "SERVER", "PROTECTIONS", "RESPONSE"],
        )
        self.assertEqual(embed.fields[2].value, f"`{len(MODULES)} / {len(MODULES)}`")
        self.assertEqual(embed.fields[3].value, "Instant")
        self.assertEqual(
            embed.image.url,
            f"attachment://{PROTECTIONS_BANNER_FILENAME}",
        )
        self.assertIn("COVERAGE", [field.name for field in embed.fields])
        self.assertEqual(
            [button.label for button in view.children],
            ["Open Panel", f"View All {len(MODULES)} Protections", "Whitelist"],
        )

    async def test_prefix_activation_transitions_to_the_detailed_panel(self):
        message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(guild=self.guild, send=AsyncMock(return_value=message))

        with patch("cogs.security.asyncio.sleep", new_callable=AsyncMock):
            await self.cog._send_all_enabled_prefix(ctx, self.cog.set_all(self.guild, True))

        first_send = ctx.send.await_args.kwargs
        self.assertEqual(first_send["file"].filename, ANTINUKE_BANNER_FILENAME)
        self.assertEqual(
            first_send["embed"].image.url,
            f"attachment://{ANTINUKE_BANNER_FILENAME}",
        )
        edit = message.edit.await_args.kwargs
        self.assertTrue(edit["embed"].title.endswith("All Protections Enabled"))
        self.assertEqual(
            [file.filename for file in edit["attachments"]],
            [PROTECTIONS_BANNER_FILENAME, PROTECTIONS_PREVIEW_FILENAME],
        )
        self.assertEqual(
            [button.label for button in edit["view"].children],
            ["Open Panel", f"View All {len(MODULES)} Protections", "Whitelist"],
        )
        first_send["file"].close()
        for file in edit["attachments"]:
            file.close()

    async def test_slash_activation_edits_the_original_response_to_the_detailed_panel(self):
        interaction = SimpleNamespace(
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with patch("cogs.security.asyncio.sleep", new_callable=AsyncMock):
            await self.cog.slash_toggle_all(interaction, True)

        first_response = interaction.response.send_message.await_args.kwargs
        self.assertEqual(first_response["file"].filename, ANTINUKE_BANNER_FILENAME)
        self.assertEqual(
            first_response["embed"].image.url,
            f"attachment://{ANTINUKE_BANNER_FILENAME}",
        )
        edit = interaction.edit_original_response.await_args.kwargs
        self.assertTrue(edit["embed"].title.endswith("All Protections Enabled"))
        self.assertEqual(
            [file.filename for file in edit["attachments"]],
            [PROTECTIONS_BANNER_FILENAME, PROTECTIONS_PREVIEW_FILENAME],
        )
        self.assertEqual(
            [button.label for button in edit["view"].children],
            ["Open Panel", f"View All {len(MODULES)} Protections", "Whitelist"],
        )
        first_response["file"].close()
        for file in edit["attachments"]:
            file.close()


if __name__ == "__main__":
    unittest.main()
