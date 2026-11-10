import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.mass_mention import ASSET_DIR, MassMentionCog


class ModerationEmojiLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_fallback_when_guild_has_no_slots(self) -> None:
        cog = MassMentionCog.__new__(MassMentionCog)
        cog._manifest = [
            {
                "key": "GAVEL",
                "name": "kryo_gavel",
                "file": "gavel_strike_red.gif",
                "fallback": "🔨",
            }
        ]
        cog.emojis = {}
        emoji = SimpleNamespace(name="existing", animated=True)
        guild = SimpleNamespace(
            id=123,
            emoji_limit=1,
            emojis=[emoji],
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_emojis_and_stickers=True)),
            create_custom_emoji=AsyncMock(),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            await cog._load_guild_emojis(guild)

        self.assertEqual(cog.emojis[123]["GAVEL"], "🔨")
        guild.create_custom_emoji.assert_not_awaited()
        self.assertIn("not have enough emoji slots", output.getvalue())

    async def test_uploads_manifest_gif_when_slot_is_available(self) -> None:
        uploaded = SimpleNamespace(
            animated=True,
            id=456,
            url="https://cdn.example/kryo_gavel.gif",
        )
        cog = MassMentionCog.__new__(MassMentionCog)
        cog._manifest = [
            {
                "key": "GAVEL",
                "name": "kryo_gavel",
                "file": "gavel_strike_red.gif",
                "fallback": "🔨",
            }
        ]
        cog.emojis = {}
        guild = SimpleNamespace(
            id=123,
            emoji_limit=1,
            emojis=[],
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_emojis_and_stickers=True)),
            create_custom_emoji=AsyncMock(return_value=uploaded),
        )

        self.assertTrue((ASSET_DIR / "gavel_strike_red.gif").is_file())
        await cog._load_guild_emojis(guild)

        self.assertEqual(cog.emojis[123]["GAVEL"], "<a:kryo_gavel:456>")
        self.assertEqual(cog.emojis[123]["GAVEL_URL"], "https://cdn.example/kryo_gavel.gif")
        guild.create_custom_emoji.assert_awaited_once()

    async def test_uses_fallback_when_manage_expressions_is_missing(self) -> None:
        cog = MassMentionCog.__new__(MassMentionCog)
        cog._manifest = [
            {
                "key": "GAVEL",
                "name": "kryo_gavel",
                "file": "gavel_strike_red.gif",
                "fallback": "🔨",
            }
        ]
        cog.emojis = {}
        guild = SimpleNamespace(
            id=123,
            emoji_limit=50,
            emojis=[],
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_emojis_and_stickers=False)),
            create_custom_emoji=AsyncMock(),
        )

        await cog._load_guild_emojis(guild)

        self.assertEqual(cog.emojis[123]["GAVEL"], "🔨")
        guild.create_custom_emoji.assert_not_awaited()
