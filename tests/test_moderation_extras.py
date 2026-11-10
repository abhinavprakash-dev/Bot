import unittest
from types import SimpleNamespace
from typing import cast

import discord
from discord.ext import commands

from cogs.staff import (
    ModerationCog,
    action_embed,
    make_purge_check,
    moderation_history_embed,
    recent_moderation_entries,
)


class FakeGuild:
    def __init__(self, entries):
        self.entries = entries

    def audit_logs(self, *, limit):
        async def iterate():
            for entry in self.entries[:limit]:
                yield entry

        return iterate()


def audit_entry(action, target_id, reason="No reason"):
    return SimpleNamespace(
        action=action,
        target=SimpleNamespace(id=target_id),
        reason=reason,
        user=None,
        created_at=discord.utils.utcnow(),
    )


class ModerationExtrasTests(unittest.IsolatedAsyncioTestCase):
    async def test_modhistory_filters_actions_and_limits_results(self):
        entries = [
            audit_entry(discord.AuditLogAction.message_delete, 10),
            audit_entry(discord.AuditLogAction.ban, 10),
            audit_entry(discord.AuditLogAction.kick, 20),
            audit_entry(discord.AuditLogAction.member_update, 10),
        ]

        result = await recent_moderation_entries(
            cast(discord.Guild, FakeGuild(entries)),
            member_id=10,
            limit=1,
        )

        self.assertEqual(result, [entries[1]])

    async def test_modhistory_embed_is_created_when_no_entries_match(self):
        member = cast(
            discord.Member,
            SimpleNamespace(mention="<@10>", display_name="Member"),
        )

        embed = moderation_history_embed(member, [])

        self.assertTrue((embed.title or "").endswith("No Recent Moderation"))

    def test_modhistory_embed_escapes_mentions_in_audit_reasons(self):
        member = cast(
            discord.Member,
            SimpleNamespace(mention="<@10>", display_name="Member"),
        )
        entry = audit_entry(discord.AuditLogAction.ban, 10, reason="@everyone")

        embed = moderation_history_embed(
            member,
            [cast(discord.AuditLogEntry, entry)],
        )
        content = " ".join(
            [embed.description or "", *(field.value or "" for field in embed.fields)]
        )

        self.assertNotIn("@everyone", content)

    def test_filtered_purge_check_excludes_invocation_and_caps_matches(self):
        check = make_purge_check(
            1,
            lambda message: message.author.bot,
            excluded_message_id=1,
        )
        invocation = cast(discord.Message, SimpleNamespace(id=1, author=SimpleNamespace(bot=True)))
        bot_message = cast(discord.Message, SimpleNamespace(id=2, author=SimpleNamespace(bot=True)))
        next_bot_message = cast(discord.Message, SimpleNamespace(id=3, author=SimpleNamespace(bot=True)))

        self.assertFalse(check(invocation))
        self.assertTrue(check(bot_message))
        self.assertFalse(check(next_bot_message))

    def test_action_embed_contains_reason_and_premium_branding(self):
        guild = cast(discord.Guild, SimpleNamespace(name="KRYO", icon=None))
        embed = action_embed(guild, "banned", "Spamming links", discord.Object(id=123))

        content = " ".join([embed.title or "", embed.description or "", *(field.value or "" for field in embed.fields)])
        self.assertIn("KRYO", content)
        self.assertIn("Spamming links", content)
        self.assertIn("Reason", content)

    def test_distinct_moderation_commands_are_registered(self):
        cog = ModerationCog(bot=cast(commands.Bot, object()), state={"warnings": {}}, save=lambda: None)

        self.assertTrue({"modhistory", "purgeuser", "purgebots"}.issubset(
            {command.name for command in cog.get_commands()}
        ))


if __name__ == "__main__":
    unittest.main()
