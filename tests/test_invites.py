import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.invites import InviteTrackerCog


class InviteTrackerTests(unittest.IsolatedAsyncioTestCase):
    def test_member_join_is_registered_as_a_discord_event_listener(self):
        self.assertTrue(InviteTrackerCog.on_member_join.__cog_listener__)

    @staticmethod
    def member(guild, member_id=300, age_days=30):
        return SimpleNamespace(
            id=member_id,
            bot=False,
            mention=f"<@{member_id}>",
            guild=guild,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )

    async def test_ready_caches_existing_invite_counts_as_baseline(self):
        inviter = SimpleNamespace(id=100)
        invite = SimpleNamespace(code="existing", uses=12, inviter=inviter)
        guild = SimpleNamespace(id=123, invites=AsyncMock(return_value=[invite]))
        bot = SimpleNamespace(guilds=[guild])
        state = {"settings": {"123": {"invite_tracker": {"enabled": True}}}}
        cog = InviteTrackerCog(bot, state, lambda: None)

        await cog.on_ready()

        self.assertEqual(cog.invite_cache[123], {"existing": (12, 100)})
        guild.invites.assert_awaited_once()

    async def test_join_attribution_and_leave_counts_are_persisted(self):
        inviter = SimpleNamespace(id=100, display_name="Inviter")
        invite = SimpleNamespace(code="hello", uses=1, inviter=inviter)
        guild = SimpleNamespace(
            id=123,
            invites=AsyncMock(return_value=[invite]),
            get_member=lambda member_id: inviter if member_id == inviter.id else None,
        )
        state = {"settings": {"123": {"invite_tracker": {
            "enabled": True,
            "members": {},
        }}}}
        saved = []
        cog = InviteTrackerCog(None, state, lambda: saved.append(True))
        cog.invite_cache[123] = {"hello": (0, inviter.id)}
        member = self.member(guild)

        await cog.on_member_join(member)

        record = state["settings"]["123"]["invite_tracker"]["members"]["300"]
        self.assertEqual(record["inviter_id"], inviter.id)
        self.assertEqual(record["invite_code"], "hello")
        self.assertFalse(record["suspicious"])
        self.assertEqual(cog.counts(guild, inviter.id), {
            "active": 1,
            "left": 0,
            "fake": 0,
            "total": 1,
        })

        await cog.on_member_remove(member)

        self.assertEqual(record["status"], "left")
        self.assertEqual(cog.counts(guild, inviter.id), {
            "active": 0,
            "left": 1,
            "fake": 0,
            "total": 1,
        })
        self.assertEqual(saved, [True, True])

    async def test_new_account_is_flagged_and_excluded_from_active_leaderboard(self):
        inviter = SimpleNamespace(id=100, display_name="Inviter")
        invite = SimpleNamespace(code="fresh", uses=1, inviter=inviter)
        guild = SimpleNamespace(
            id=123,
            invites=AsyncMock(return_value=[invite]),
            get_member=lambda member_id: inviter if member_id == inviter.id else None,
        )
        state = {"settings": {"123": {"invite_tracker": {
            "enabled": True,
            "minimum_account_age_days": 7,
            "members": {},
        }}}}
        cog = InviteTrackerCog(None, state, lambda: None)
        cog.invite_cache[123] = {"fresh": (0, inviter.id)}

        await cog.on_member_join(self.member(guild, member_id=301, age_days=1))

        counts = cog.counts(guild, inviter.id)
        self.assertEqual(counts["fake"], 1)
        self.assertEqual(counts["active"], 0)
        self.assertEqual(cog.leaderboard(guild)[0]["active"], 0)

    async def test_simultaneous_invite_use_is_left_unattributed(self):
        inviter_one = SimpleNamespace(id=100)
        inviter_two = SimpleNamespace(id=200)
        invites = [
            SimpleNamespace(code="one", uses=1, inviter=inviter_one),
            SimpleNamespace(code="two", uses=1, inviter=inviter_two),
        ]
        guild = SimpleNamespace(id=123, invites=AsyncMock(return_value=invites))
        state = {"settings": {"123": {"invite_tracker": {
            "enabled": True,
            "members": {},
        }}}}
        cog = InviteTrackerCog(None, state, lambda: None)
        cog.invite_cache[123] = {"one": (0, inviter_one.id), "two": (0, inviter_two.id)}

        await cog.on_member_join(self.member(guild))

        self.assertIsNone(state["settings"]["123"]["invite_tracker"]["members"]["300"]["inviter_id"])
        self.assertEqual(cog.leaderboard(guild), [])

    def test_invite_embeds_use_branded_fields_and_server_context(self):
        inviter = SimpleNamespace(id=100, display_name="Top inviter")
        guild = SimpleNamespace(
            id=123,
            name="KRYO Community",
            icon=None,
            member_count=2500,
            get_member=lambda member_id: inviter if member_id == inviter.id else None,
        )
        member = SimpleNamespace(
            id=100,
            display_name="Top inviter",
            mention="<@100>",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/member.png"),
        )
        cog = InviteTrackerCog(
            SimpleNamespace(user=SimpleNamespace(display_avatar=SimpleNamespace(
                url="https://cdn.example.com/bot.png",
            ))),
            {"settings": {"123": {"invite_tracker": {
                "members": {
                    "201": {"inviter_id": 100, "status": "active", "suspicious": False},
                    "202": {"inviter_id": 100, "status": "left", "suspicious": False},
                    "203": {"inviter_id": 100, "status": "active", "suspicious": True},
                },
            }}}},
            lambda: None,
        )

        profile = cog.member_embed(guild, member)
        leaderboard = cog.leaderboard_embed(guild)

        self.assertEqual(len(profile.fields), 4)
        self.assertIn("33%", profile.fields[-1].value)
        self.assertEqual(profile.thumbnail.url, "https://cdn.example.com/member.png")
        self.assertIn("KRYO  •  INVITE TRACKER", profile.footer.text)
        self.assertIn("Top inviter", leaderboard.description)
        self.assertIn("1 active", leaderboard.description)
        self.assertEqual(leaderboard.thumbnail.url, "https://cdn.example.com/bot.png")
        self.assertIn("COMMUNITY RANKINGS", leaderboard.author.name)


if __name__ == "__main__":
    unittest.main()
