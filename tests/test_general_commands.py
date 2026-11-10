import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

from cogs.general import (
    NoPrefixCog,
    UtilityCog,
    announcement_embed,
    parse_choices,
    parse_color,
    parse_dice,
    parse_snowflake,
    transform_text,
)


class NoPrefixCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_access_uses_animated_icon_and_updated_copy(self):
        icon = "<a:kryo_globe:123456789012345678>"
        mass_mention_cog = SimpleNamespace(emojis={456: {"GLOBE": icon}})
        bot = SimpleNamespace(get_cog=lambda name: mass_mention_cog if name == "MassMentionCog" else None)
        cog = NoPrefixCog(bot, {"noprefix": [123]}, Mock())
        ctx = SimpleNamespace(guild=SimpleNamespace(id=456), send=AsyncMock())
        user = SimpleNamespace(id=123, mention="<@123>")

        await NoPrefixCog.np_add.callback(cog, ctx, user)

        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, f"{icon}  NO-PREFIX · ALREADY ACTIVE")
        self.assertIn("**Access is already active.**", embed.description)
        self.assertIn("use KRYO commands without a prefix", embed.description)
        self.assertNotIn("emoji_1", embed.title)

    async def test_duplicate_access_uses_styled_fallback_when_animation_is_unavailable(self):
        bot = SimpleNamespace(get_cog=lambda _name: None)
        cog = NoPrefixCog(bot, {"noprefix": [123]}, Mock())
        ctx = SimpleNamespace(guild=SimpleNamespace(id=456), send=AsyncMock())
        user = SimpleNamespace(id=123, mention="<@123>")

        await NoPrefixCog.np_add.callback(cog, ctx, user)

        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "✧ NO-PREFIX · ALREADY ACTIVE")


class UtilityCommandTests(unittest.TestCase):
    def test_announcement_slash_command_is_registered(self):
        cog = UtilityCog(cast(commands.Bot, object()))
        commands_by_name = {command.name: command for command in cog.get_app_commands()}
        self.assertIn("announce", commands_by_name)
        self.assertTrue(commands_by_name["announce"].guild_only)

    def test_announcement_embed_uses_server_branding_and_optional_title(self):
        guild = Mock(spec=discord.Guild)
        guild.name = "KRYO"
        guild.icon = None

        embed = announcement_embed(guild, "Server update", "Maintenance")

        self.assertEqual(embed.title, "Maintenance")
        self.assertEqual(embed.description, "Server update")
        self.assertEqual(embed.author.name, "KRYO Announcement")

    def test_added_actions_are_registered_once_as_hybrid_commands(self):
        cog = UtilityCog(cast(commands.Bot, object()))
        command_names = [command.name for command in cog.get_commands()]
        added_actions = {
            "servericon",
            "serverbanner",
            "roleinfo",
            "channelinfo",
            "permissions",
            "roles",
            "channels",
            "inrole",
            "emojis",
            "stickers",
            "choose",
            "coinflip",
            "roll",
            "eightball",
            "timestamp",
            "joinedat",
            "createdat",
            "toprole",
            "rolecount",
            "channelcount",
            "humancount",
            "botcount",
            "boosters",
            "colorinfo",
            "snowflake",
            "textcase",
            "charcount",
            "wordcount",
            "jsonformat",
            "base64",
            "urlencode",
            "urldecode",
            "unicode",
        }

        self.assertTrue(added_actions.issubset(command_names))
        self.assertEqual(len(command_names), len(set(command_names)))
        commands_by_name = {command.name: command for command in cog.get_commands()}
        self.assertTrue(all(commands_by_name[name].app_command for name in added_actions))

    def test_dice_parser_accepts_bounded_dice_notation(self):
        self.assertEqual(parse_dice("d20"), (1, 20))
        self.assertEqual(parse_dice(" 3D6 "), (3, 6))
        for expression in ("0d6", "101d6", "d1", "d10001", "2d6+5", "d20xyz"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parse_dice(expression)

    def test_choice_parser_limits_count_and_length(self):
        self.assertEqual(parse_choices("red | blue, green"), ["red", "blue", "green"])
        for choices in ("one", ",".join(str(index) for index in range(21)), "x" * 101 + ", other"):
            with self.subTest(choices=choices[:20]), self.assertRaises(ValueError):
                parse_choices(choices)

    def test_hex_color_parser_requires_exactly_six_hex_digits(self):
        self.assertEqual(parse_color("#8f80f7"), 0x8F80F7)
        self.assertEqual(parse_color("0x5865f2"), 0x5865F2)
        for color in ("#fff", "purple", "12345g", "12345678"):
            with self.subTest(color=color), self.assertRaises(ValueError):
                parse_color(color)

    def test_snowflake_parser_rejects_non_discord_ids(self):
        self.assertEqual(parse_snowflake("123"), 123)
        for snowflake in ("0", "-1", "abc", str(2**64)):
            with self.subTest(snowflake=snowflake), self.assertRaises(ValueError):
                parse_snowflake(snowflake)

    def test_text_case_transform_supports_documented_styles_only(self):
        self.assertEqual(transform_text("hello WORLD", "upper"), "HELLO WORLD")
        self.assertEqual(transform_text("hello WORLD", "title"), "Hello World")
        with self.assertRaises(ValueError):
            transform_text("hello", "unknown")


if __name__ == "__main__":
    unittest.main()
