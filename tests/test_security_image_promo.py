import asyncio
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from botcore.security.image_promo import looks_like_gambling_promotion
from cogs.security import SecurityCog


class ImagePromotionDetectionTests(unittest.TestCase):
    def test_detects_casino_bonus_ads(self):
        text = "SLOTS BONUSES Activate Code for Bonus PLATINUM Deposit Withdraw"

        self.assertTrue(looks_like_gambling_promotion(text))

    def test_does_not_flag_generic_game_rewards_or_financial_promotions(self):
        self.assertFalse(looks_like_gambling_promotion("Game event rewards and bonus items"))
        self.assertFalse(looks_like_gambling_promotion("Bank deposit bonus, activate your account"))
        self.assertFalse(looks_like_gambling_promotion("Casino game screenshot"))
        self.assertFalse(
            looks_like_gambling_promotion("Games bonuses activate code for event rewards")
        )

    def test_detects_game_promotion_with_bonus_activation_and_withdrawal(self):
        ocr_text = "BONUSES GAMES Activate Code for Bonus WITHDRAW"

        self.assertTrue(looks_like_gambling_promotion(ocr_text))

    def test_detects_activation_code_withdrawal_ad_without_game_word(self):
        ocr_text = "BONUSES Activate Code for Bonus WITHDRAW"

        self.assertTrue(looks_like_gambling_promotion(ocr_text))

    def test_does_not_flag_ordinary_bank_account_activation(self):
        self.assertFalse(
            looks_like_gambling_promotion(
                "Bank deposit bonus. Activate your account with this code."
            )
        )


class ImagePromotionScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_promotional_image_is_deleted_and_user_is_not_punished(self):
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="promo.png",
            size=2048,
            read=AsyncMock(return_value=b"image-bytes"),
        )
        message = SimpleNamespace(
            attachments=[attachment],
            guild=SimpleNamespace(id=1),
            id=2,
            channel=object(),
            delete=AsyncMock(),
        )
        member = SimpleNamespace(id=3, mention="<@3>")
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog._promo_image_scans = asyncio.Semaphore(1)
        cog.notify = AsyncMock()
        cog.global_protected_owner_ids = frozenset()

        with patch(
            "cogs.security.image_promo_mod.extract_image_text",
            return_value="Casino bonus activate",
        ):
            await cog._check_promotional_images(message, member)

        message.delete.assert_awaited_once()
        cog.notify.assert_awaited_once()

    async def test_forwarded_image_is_scanned_from_message_snapshot(self):
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="forwarded-promo.png",
            size=2048,
            read=AsyncMock(return_value=b"image-bytes"),
        )
        message = SimpleNamespace(
            attachments=[],
            message_snapshots=[SimpleNamespace(attachments=[attachment])],
            guild=SimpleNamespace(id=1),
            id=2,
            channel=object(),
            delete=AsyncMock(),
        )
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog._promo_image_scans = asyncio.Semaphore(1)
        cog.notify = AsyncMock()
        cog.global_protected_owner_ids = frozenset()

        with patch(
            "cogs.security.image_promo_mod.extract_image_text",
            return_value="GAMES BONUSES Activate Code WITHDRAW",
        ):
            await cog._check_promotional_images(message, SimpleNamespace(id=3, mention="<@3>"))

        attachment.read.assert_awaited_once()
        message.delete.assert_awaited_once()

    async def test_scans_fourth_attachment_for_promotional_text(self):
        attachments = [
            SimpleNamespace(
                content_type="image/png",
                filename=f"image-{index}.png",
                size=2048,
                read=AsyncMock(return_value=f"image-{index}".encode()),
            )
            for index in range(4)
        ]
        message = SimpleNamespace(
            attachments=attachments,
            message_snapshots=[],
            guild=SimpleNamespace(id=1),
            id=2,
            channel=object(),
            delete=AsyncMock(),
        )
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog._promo_image_scans = asyncio.Semaphore(1)
        cog.notify = AsyncMock()
        cog.global_protected_owner_ids = frozenset()

        with patch(
            "cogs.security.image_promo_mod.extract_image_text",
            side_effect=[
                "ordinary screenshot",
                "ordinary screenshot",
                "ordinary screenshot",
                "BONUSES Activate Code WITHDRAW",
            ],
        ):
            await cog._check_promotional_images(message, SimpleNamespace(id=3, mention="<@3>"))

        for attachment in attachments:
            attachment.read.assert_awaited_once()
        message.delete.assert_awaited_once()

    async def test_admin_image_is_scanned_before_other_admin_exemptions(self):
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="promo.png",
            size=2048,
            read=AsyncMock(return_value=b"image-bytes"),
        )
        member = Mock(spec=discord.Member)
        member.id = 3
        member.bot = False
        member.guild_permissions = SimpleNamespace(administrator=True)
        member.roles = []
        member.mention = "<@3>"
        message = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=member,
            content="",
            attachments=[attachment],
            message_snapshots=[],
        )
        background = []
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog.has_tier = lambda guild_id, tier: True
        cog.enabled = lambda guild_id, module: module == "antipromo"
        cog.whitelisted = lambda guild, user_id, module, member, role_ids: False
        cog.is_protected = lambda guild, user_id: False
        cog._check_promotional_images = AsyncMock()
        cog._spawn = lambda coroutine, label: background.append(coroutine)

        await cog._check_message(message)
        self.assertEqual(len(background), 1)
        self.assertEqual(len(background), 1)
        await background.pop()
        cog._check_promotional_images.assert_awaited_once_with(message, member)

    async def test_large_image_is_skipped_without_downloading(self):
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="large.png",
            size=9 * 1024 * 1024,
            read=AsyncMock(),
        )
        message = SimpleNamespace(
            attachments=[attachment],
            guild=SimpleNamespace(id=1),
            id=2,
            channel=object(),
            delete=AsyncMock(),
        )
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog._promo_image_scans = asyncio.Semaphore(1)
        cog.notify = AsyncMock()
        cog.global_protected_owner_ids = frozenset()

        await cog._check_promotional_images(message, SimpleNamespace(id=3, mention="<@3>"))

        attachment.read.assert_not_awaited()
        message.delete.assert_not_awaited()


class RaidBotMessageTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, *, whitelisted=False):
        background = []
        cog = SecurityCog.__new__(SecurityCog)
        cog.global_protected_owner_ids = frozenset()
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=10))
        cog.has_tier = lambda guild_id, tier: True
        cog.enabled = lambda guild_id, module: True
        cog.is_protected = lambda guild, user_id: user_id == 10
        cog.whitelisted = lambda guild, user_id, module: whitelisted
        cog._spawn = lambda coroutine, label: background.append(coroutine)
        cog.write_log = AsyncMock()
        return cog, background

    def make_raid_message(self, events):
        async def delete():
            events.append("delete")

        async def ban(user, *, reason, delete_message_seconds):
            events.append("ban")

        guild = SimpleNamespace(id=1, ban=AsyncMock(side_effect=ban))
        author = SimpleNamespace(id=99, bot=True)
        message = SimpleNamespace(
            guild=guild,
            author=author,
            content="@everyone Raided by https://discord.gg/resurgence",
            mention_everyone=True,
            delete=AsyncMock(side_effect=delete),
        )
        return message, guild

    async def test_bot_mass_invite_is_deleted_then_bot_banned_and_logged(self):
        events = []
        message, guild = self.make_raid_message(events)
        cog, background = self.make_cog()

        await cog._check_message(message)

        self.assertEqual(events, ["delete", "ban"])
        guild.ban.assert_awaited_once_with(
            message.author,
            reason="Raid prevention: bot posted a mass mention",
            delete_message_seconds=0,
        )
        self.assertEqual(len(background), 1)
        await background.pop()
        cog.write_log.assert_awaited_once()

    async def test_bot_mass_mention_is_detected_when_message_content_is_unavailable(self):
        events = []
        message, guild = self.make_raid_message(events)
        message.content = ""
        message.mention_everyone = True
        cog, background = self.make_cog()

        await cog._check_message(message)

        self.assertEqual(events, ["delete", "ban"])
        guild.ban.assert_awaited_once()
        self.assertEqual(len(background), 1)
        await background.pop()

    async def test_regular_bot_messages_are_not_removed_or_banned(self):
        message, guild = self.make_raid_message([])
        message.content = "A regular bot status update"
        message.mention_everyone = False
        cog, background = self.make_cog()

        await cog._check_message(message)

        message.delete.assert_not_awaited()
        guild.ban.assert_not_awaited()
        self.assertEqual(background, [])

    async def test_whitelisted_bot_is_not_removed_or_banned(self):
        message, guild = self.make_raid_message([])
        cog, background = self.make_cog(whitelisted=True)

        await cog._check_message(message)

        message.delete.assert_not_awaited()
        guild.ban.assert_not_awaited()
        self.assertEqual(background, [])

    async def test_whitelist_for_one_enabled_module_does_not_bypass_another(self):
        message, guild = self.make_raid_message([])
        cog, background = self.make_cog()
        cog.whitelisted = lambda guild, user_id, module: module == "antipromo"

        await cog._check_message(message)

        message.delete.assert_awaited_once()
        guild.ban.assert_awaited_once()
        self.assertEqual(len(background), 1)
        await background.pop()


if __name__ == "__main__":
    unittest.main()
