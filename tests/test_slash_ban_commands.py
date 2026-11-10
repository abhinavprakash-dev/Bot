import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.staff import ModerationCog


class SlashBanCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.user = SimpleNamespace(
            id=123,
            name="TestUser",
            display_avatar=SimpleNamespace(url="https://cdn.example/bot.png"),
            send=AsyncMock(),
        )
        self.bot = SimpleNamespace(
            user=self.user,
            get_cog=Mock(return_value=None),
            fetch_user=AsyncMock(return_value=self.user),
        )
        self.cog = ModerationCog(self.bot, {}, lambda: None)
        self.response = SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                name="Test Server",
                icon=None,
                ban=AsyncMock(),
                unban=AsyncMock(),
            ),
            user=SimpleNamespace(id=456, name="Moderator"),
            response=self.response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_slash_ban_accepts_reason_and_bans_selected_member(self) -> None:
        member = SimpleNamespace(
            id=123,
            name="TestUser",
            display_avatar=SimpleNamespace(url="https://cdn.example/user.png"),
            send=AsyncMock(),
        )

        with (
            patch("cogs.staff.member_problem", return_value=None),
            patch("cogs.staff.try_dm", new=AsyncMock(return_value=True)),
        ):
            await ModerationCog.ban_slash.callback(
                self.cog,
                self.interaction,
                member,
                "Repeated spam",
            )

        self.interaction.guild.ban.assert_awaited_once_with(
            member,
            reason="Repeated spam | by namespace(id=456, name='Moderator')",
        )
        self.response.send_message.assert_awaited_once()
        sent_embed = self.response.send_message.await_args.kwargs["embed"]
        self.assertIn("**Reason** — Repeated spam", sent_embed.description)

    async def test_slash_unban_takes_id_and_reason(self) -> None:
        await ModerationCog.unban_slash.callback(
            self.cog,
            self.interaction,
            "123",
            "Appeal accepted",
        )

        self.interaction.guild.unban.assert_awaited_once()
        target, = self.interaction.guild.unban.await_args.args
        self.assertEqual(target.id, 123)
        self.assertEqual(
            self.interaction.guild.unban.await_args.kwargs["reason"],
            "Appeal accepted | by namespace(id=456, name='Moderator')",
        )
        self.user.send.assert_awaited_once()
        self.response.send_message.assert_awaited_once()
        sent_embed = self.response.send_message.await_args.kwargs["embed"]
        self.assertIn("**TestUser** was unbanned.", sent_embed.description)

    async def test_slash_unban_rejects_non_numeric_id_without_api_call(self) -> None:
        await ModerationCog.unban_slash.callback(
            self.cog,
            self.interaction,
            "not-an-id",
            "Reason",
        )

        self.interaction.guild.unban.assert_not_awaited()
        self.bot.fetch_user.assert_not_awaited()
        self.response.send_message.assert_awaited_once()
        self.assertIn(
            "Invalid User ID",
            self.response.send_message.await_args.kwargs["embed"].title,
        )

    def test_slash_commands_are_registered_with_reason_options(self) -> None:
        self.assertEqual(ModerationCog.ban_slash.name, "ban")
        self.assertEqual(ModerationCog.unban_slash.name, "unban")
        self.assertIn("reason", {parameter.name for parameter in ModerationCog.ban_slash.parameters})
        self.assertIn("reason", {parameter.name for parameter in ModerationCog.unban_slash.parameters})
