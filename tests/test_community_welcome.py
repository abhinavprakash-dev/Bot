from io import BytesIO
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image

from cogs.community import WelcomerCog


class WelcomeMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_leave_message_uses_separate_channel_when_configured(self):
        class TextChannel:
            def __init__(self):
                self.send = AsyncMock()

        welcome_channel = TextChannel()
        leave_channel = TextChannel()
        guild = SimpleNamespace(
            id=123,
            name="KRYO",
            member_count=42,
            get_channel=lambda channel_id: {
                456: welcome_channel,
                789: leave_channel,
            }.get(channel_id),
        )
        member = SimpleNamespace(
            guild=guild,
            mention="<@789>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
        )
        cog = WelcomerCog(
            None,
            {"settings": {"123": {"welcome": {
                "channel_id": 456,
                "leave_channel_id": 789,
                "leave_enabled": True,
                "leave_message": "Goodbye {name}",
            }}}},
            lambda: None,
        )

        with patch("cogs.community.discord.TextChannel", TextChannel):
            await cog.on_member_remove(member)

        welcome_channel.send.assert_not_awaited()
        leave_channel.send.assert_awaited_once()
        self.assertEqual(
            leave_channel.send.await_args.kwargs["embed"].description,
            "Goodbye Aarav",
        )

    async def test_leave_message_falls_back_to_welcome_channel(self):
        class TextChannel:
            def __init__(self):
                self.send = AsyncMock()

        welcome_channel = TextChannel()
        guild = SimpleNamespace(
            id=123,
            name="KRYO",
            member_count=42,
            get_channel=lambda channel_id: welcome_channel if channel_id == 456 else None,
        )
        member = SimpleNamespace(
            guild=guild,
            mention="<@789>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
        )
        cog = WelcomerCog(
            None,
            {"settings": {"123": {"welcome": {
                "channel_id": 456,
                "leave_enabled": True,
                "leave_message": "Goodbye {name}",
            }}}},
            lambda: None,
        )

        with patch("cogs.community.discord.TextChannel", TextChannel):
            await cog.on_member_remove(member)

        welcome_channel.send.assert_awaited_once()

    async def test_welcome_markup_is_rendered_inside_the_embed(self):
        message = (
            "<a:kenji_crown_3:1422605209141510305> __WELCOME__\n"
            "<#1532616754814255203> {user} {server} #{count}"
        )
        state = {
            "settings": {
                "123": {
                    "welcome": {
                        "enabled": True,
                        "channel_id": 456,
                        "message": message,
                        "image_url": "https://cdn.example.com/welcome.png",
                    },
                },
            },
        }
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=123, name="KRYO", member_count=42)
        member = SimpleNamespace(
            guild=guild,
            mention="<@789>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
        )
        cog = WelcomerCog(None, state, lambda: None)
        cog.channel_for = lambda _guild: channel

        await cog.on_member_join(member)

        channel.send.assert_awaited_once()
        sent = channel.send.await_args.kwargs
        self.assertNotIn("content", sent)
        self.assertEqual(
            sent["embed"].description,
            "<a:kenji_crown_3:1422605209141510305> __WELCOME__\n"
            "<#1532616754814255203> <@789> KRYO #42",
        )
        self.assertEqual(
            sent["embed"].image.url,
            "https://cdn.example.com/welcome.png",
        )
        mentions = sent["allowed_mentions"]
        self.assertTrue(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.everyone)

    def test_image_card_style_generates_discord_png_and_video_link(self):
        state = {"settings": {"123": {"welcome": {
            "style": "image_card",
            "font_size": 48,
            "title_font_size": 64,
            "title": "Welcome {name}",
            "video_url": "https://youtu.be/example",
            "image_url": "https://cdn.example.com/welcome.png",
            "color": "#2ECC71",
        }}}}
        guild = SimpleNamespace(id=123, name="KRYO", member_count=42, emojis=[])
        member = SimpleNamespace(
            guild=guild,
            mention="<@789>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
        )
        cog = WelcomerCog(None, state, lambda: None)

        with patch.object(cog, "card_font", wraps=cog.card_font) as card_font:
            payload = cog.welcome_payload(member, "Welcome {user} to {server}!")
        card_font.assert_any_call(64, bold=True)
        card_font.assert_any_call(48)

        self.assertEqual(payload["embed"].image.url, "attachment://welcome-card.png")
        self.assertEqual(payload["embed"].thumbnail.url, "https://cdn.example.com/welcome.png")
        self.assertEqual(payload["content"], "Watch the welcome video: https://youtu.be/example")
        self.assertEqual(payload["file"].filename, "welcome-card.png")
        self.assertTrue(payload["embed"].title.endswith("Welcome Aarav"))
        self.assertEqual(payload["embed"].color.value, 0x2ECC71)
        image = Image.open(BytesIO(payload["file"].fp.getvalue()))
        self.assertEqual(image.size, (1200, 480))
        self.assertGreater(image.getpixel((600, 100))[1], image.getpixel((600, 100))[0] * 2)

    async def test_plain_welcome_and_optional_dm_use_saved_video_link(self):
        channel = SimpleNamespace(send=AsyncMock())
        member = SimpleNamespace(
            id=789,
            guild=SimpleNamespace(id=123, name="KRYO", member_count=42, emojis=[]),
            mention="<@789>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
            send=AsyncMock(),
        )
        state = {"settings": {"123": {"welcome": {
            "enabled": True,
            "channel_id": 456,
            "style": "plain",
            "dm_enabled": True,
            "video_url": "https://youtu.be/example",
            "message": "Hello {name}!",
        }}}}
        cog = WelcomerCog(None, state, lambda: None)
        cog.channel_for = lambda _guild: channel

        await cog.on_member_join(member)

        self.assertIn("Hello Aarav!", channel.send.await_args.kwargs["content"])
        self.assertIn("https://youtu.be/example", channel.send.await_args.kwargs["content"])
        member.send.assert_awaited_once()
        self.assertEqual(member.send.await_args.kwargs["content"], channel.send.await_args.kwargs["content"])


if __name__ == "__main__":
    unittest.main()
