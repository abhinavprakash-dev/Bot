import unittest
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.jail import JAILED_CHANNEL_PERMISSIONS, JailActionError, JailCog


class FakeRole:
    def __init__(self, role_id, position=1):
        self.id = role_id
        self.position = position
        self.name = "KRYO Jail"

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeMember:
    def __init__(self, member_id, roles=None, position=1, administrator=False):
        self.id = member_id
        self.name = f"username-{member_id}"
        self.global_name = None
        self.display_name = f"Member {member_id}"
        self.mention = f"<@{member_id}>"
        self.roles = list(roles or [])
        self.top_role = FakeRole(member_id, position)
        self.guild_permissions = SimpleNamespace(
            administrator=administrator,
            manage_roles=False,
        )
        self.add_roles = AsyncMock(side_effect=self._add_role)
        self.remove_roles = AsyncMock(side_effect=self._remove_role)

    def _add_role(self, role, **_kwargs):
        if role not in self.roles:
            self.roles.append(role)

    def _remove_role(self, role, **_kwargs):
        if role in self.roles:
            self.roles.remove(role)


class FakeChannel:
    def __init__(self, channel_id, category=None, permissions_synced=False):
        self.id = channel_id
        self.category = category
        self.permissions_synced = permissions_synced
        self.overwrites = {}
        self.set_permissions = AsyncMock(side_effect=self._set_permissions)

    def overwrites_for(self, member):
        return copy(self.overwrites.get(member.id, discord.PermissionOverwrite()))

    def _set_permissions(self, member, *, overwrite, **_kwargs):
        if overwrite is None:
            self.overwrites.pop(member.id, None)
        else:
            self.overwrites[member.id] = copy(overwrite)


class FakeGuild:
    def __init__(self):
        self.id = 123
        self.owner_id = 1
        self.role = FakeRole(900, position=5)
        self.bot_member = FakeMember(2, position=10)
        self.bot_member.guild_permissions.manage_roles = True
        self.member = FakeMember(3, position=1)
        self.member.guild = self
        self.members = [self.member]
        self.category = FakeChannel(10)
        self.channel = FakeChannel(11, category=self.category, permissions_synced=True)
        self.channels = [self.category, self.channel]
        self.category.guild = self
        self.channel.guild = self
        self.me = self.bot_member
        self.get_role = lambda role_id: self.role if role_id == self.role.id else None
        self.get_member = lambda member_id: self.member if member_id == self.member.id else None
        self.fetch_member = AsyncMock(return_value=self.member)
        self.create_role = AsyncMock(return_value=self.role)


class JailCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.state = {"settings": {}}
        self.saved = []
        self.cog = JailCog(SimpleNamespace(guilds=[self.guild]), self.state, lambda: self.saved.append(True))

    async def test_jail_denies_text_and_voice_without_changing_visibility(self):
        member = self.guild.member
        self.guild.category.overwrites[member.id] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            connect=True,
        )

        record = await self.cog.jail_member(self.guild, member.id, "Reviewing a report", "Moderator")

        overwrite = self.guild.category.overwrites[member.id]
        self.assertTrue(overwrite.view_channel)
        for permission in JAILED_CHANNEL_PERMISSIONS:
            self.assertFalse(getattr(overwrite, permission))
        self.assertEqual(record["overwrites"]["10"]["send_messages"], True)
        self.assertIn(self.guild.role, member.roles)
        self.assertIn(str(member.id), self.cog.cfg(self.guild.id)["members"])
        self.assertEqual(len(self.saved), 2)
        self.guild.channel.set_permissions.assert_not_awaited()

    async def test_unjail_restores_original_member_permissions_and_removes_role(self):
        member = self.guild.member
        original = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            connect=False,
        )
        self.guild.category.overwrites[member.id] = original
        record = await self.cog.jail_member(self.guild, member.id, "", "Moderator")
        saved_before_release = len(self.saved)

        await self.cog.unjail_member(self.guild, member.id, "Administrator")

        restored = self.guild.category.overwrites[member.id]
        self.assertEqual(restored.view_channel, True)
        self.assertEqual(restored.send_messages, True)
        self.assertEqual(restored.connect, False)
        self.assertEqual(self.cog.cfg(self.guild.id)["members"], {})
        self.assertNotIn(self.guild.role, member.roles)
        self.assertEqual(len(self.saved), saved_before_release + 1)
        self.assertIsNotNone(record)

    async def test_jail_rejects_server_administrator(self):
        self.guild.member.guild_permissions.administrator = True

        with self.assertRaisesRegex(JailActionError, "Administrator permission cannot be jailed"):
            await self.cog.jail_member(self.guild, self.guild.member.id, "", "Moderator")

        self.guild.create_role.assert_not_awaited()

    async def test_jail_rejects_target_at_or_above_bot_role(self):
        self.guild.member.top_role = FakeRole(3, position=10)

        with self.assertRaisesRegex(JailActionError, "above the member's highest role"):
            await self.cog.jail_member(self.guild, self.guild.member.id, "", "Moderator")

    async def test_jail_accepts_exact_username_and_member_id(self):
        member = await self.cog.resolve_member_reference(self.guild, self.guild.member.name)
        by_id = await self.cog.resolve_member_reference(self.guild, self.guild.member.id)

        self.assertIs(member, self.guild.member)
        self.assertIs(by_id, self.guild.member)

    async def test_jail_username_lookup_rejects_ambiguous_display_names(self):
        duplicate = FakeMember(4, position=1)
        duplicate.display_name = self.guild.member.display_name
        self.guild.members.append(duplicate)

        with self.assertRaisesRegex(JailActionError, "matches multiple"):
            await self.cog.resolve_member_reference(self.guild, self.guild.member.display_name)

    def test_jail_and_unjail_are_registered_as_hybrid_prefix_and_slash_commands(self):
        commands_by_name = {command.name: command for command in self.cog.get_commands()}

        self.assertIn("jail", commands_by_name)
        self.assertIn("unjail", commands_by_name)
        for command_name in ("jail", "unjail"):
            command = commands_by_name[command_name]
            self.assertIsNotNone(command.app_command)
            self.assertTrue(command.app_command.default_permissions.manage_roles)
        self.assertIn("release", commands_by_name["unjail"].aliases)

    async def test_member_already_in_voice_requires_move_members_permission(self):
        self.guild.member.voice = SimpleNamespace(channel=object())
        self.guild.bot_member.guild_permissions.move_members = False

        with self.assertRaisesRegex(JailActionError, "Move Members permission"):
            await self.cog.jail_member(self.guild, self.guild.member.id, "", "Moderator")

        self.guild.create_role.assert_not_awaited()

    async def test_new_channel_gets_jail_restrictions_for_jailed_members(self):
        member = self.guild.member
        await self.cog.jail_member(self.guild, member.id, "", "Moderator")
        new_channel = FakeChannel(12)
        new_channel.guild = self.guild
        self.guild.channels.append(new_channel)

        await self.cog.on_guild_channel_create(new_channel)

        overwrite = new_channel.overwrites[member.id]
        self.assertIsNone(overwrite.view_channel)
        self.assertFalse(overwrite.send_messages)
        self.assertFalse(overwrite.connect)
        self.assertIn("12", self.cog.cfg(self.guild.id)["members"][str(member.id)]["overwrites"])

    async def test_new_synced_channel_inherits_restrictions_without_becoming_unsynced(self):
        member = self.guild.member
        await self.cog.jail_member(self.guild, member.id, "", "Moderator")
        new_channel = FakeChannel(12, category=self.guild.category, permissions_synced=True)
        new_channel.guild = self.guild
        self.guild.channels.append(new_channel)

        await self.cog.on_guild_channel_create(new_channel)

        new_channel.set_permissions.assert_not_awaited()
        self.assertNotIn("12", self.cog.cfg(self.guild.id)["members"][str(member.id)]["overwrites"])

    async def test_incomplete_rollback_keeps_member_in_jail_list(self):
        failing_channel = FakeChannel(12)
        failing_channel.guild = self.guild
        failing_channel.set_permissions.side_effect = discord.Forbidden(MagicMock(), "forbidden")
        self.guild.channels.append(failing_channel)

        with self.assertRaisesRegex(JailActionError, "partially applied"):
            await self.cog.jail_member(self.guild, self.guild.member.id, "", "Moderator")

        self.assertIn(str(self.guild.member.id), self.cog.cfg(self.guild.id)["members"])
        self.assertIn(failing_channel.id, [channel.id for channel in self.guild.channels])

    async def test_returning_jailed_member_gets_jail_role_and_channel_overwrites(self):
        member = self.guild.member
        await self.cog.jail_member(self.guild, member.id, "", "Moderator")
        member.roles.remove(self.guild.role)
        member.add_roles.reset_mock()

        await self.cog.on_member_join(member)

        self.assertIn(self.guild.role, member.roles)
        member.add_roles.assert_awaited_once()
        self.assertFalse(self.guild.category.overwrites[member.id].send_messages)


if __name__ == "__main__":
    unittest.main()
