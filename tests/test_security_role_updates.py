import unittest
from collections import defaultdict, deque
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from botcore.security import rules
from cogs.security import SecurityCog


def make_role(role_id, *, administrator=False):
    return SimpleNamespace(
        id=role_id,
        name=f"role-{role_id}",
        mention=f"<@&{role_id}>",
        permissions=discord.Permissions(administrator=administrator),
        managed=False,
    )


def make_entry(user_id, target_id, *, added=(), removed=()):
    return SimpleNamespace(
        action=discord.AuditLogAction.member_role_update,
        user_id=user_id,
        target=SimpleNamespace(id=target_id, name=f"member-{target_id}"),
        created_at=discord.utils.utcnow(),
        extra=None,
        before={"roles": list(removed)},
        after={"roles": list(added)},
    )


class RoleClassifierTests(unittest.TestCase):
    def test_self_assigned_non_dangerous_role_is_not_instant(self):
        role = make_role(501)
        result = rules.classify_member_role_update(
            make_entry(10, 10, added=[role]),
            added=[role],
            removed=[],
        )

        if result is None:
            self.fail("Expected a non-dangerous role-change detection")
        self.assertFalse(result.instant)
        self.assertFalse(result.escalation)

    def test_dangerous_role_grant_is_instant(self):
        role = make_role(501, administrator=True)
        result = rules.classify_member_role_update(
            make_entry(20, 10, added=[role]),
            added=[role],
            removed=[],
        )

        if result is None:
            self.fail("Expected a dangerous role-grant detection")
        self.assertTrue(result.instant)

    def test_moderation_permission_role_grant_is_dangerous(self):
        role = SimpleNamespace(
            id=501,
            name="moderator",
            mention="<@&501>",
            permissions=discord.Permissions(moderate_members=True),
            managed=False,
        )
        result = rules.classify_member_role_update(
            make_entry(20, 10, added=[role]),
            added=[role],
            removed=[],
        )

        if result is None:
            self.fail("Expected a dangerous role-grant detection")
        self.assertTrue(result.instant)

    def test_removing_dangerous_role_is_not_treated_as_an_incident(self):
        role = make_role(501, administrator=True)
        result = rules.classify_member_role_update(
            make_entry(20, 10, removed=[role]),
            added=[],
            removed=[role],
        )

        self.assertIsNone(result)


class RoleUpdateHandlerTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, actors, trusted_ids=()):
        cog = SecurityCog.__new__(SecurityCog)
        cog.bot = cast(commands.Bot, SimpleNamespace(user=SimpleNamespace(id=999)))
        cog.trusted_role_management_bot_ids = frozenset(trusted_ids)
        cog.enabled = Mock(return_value=True)
        cog.role_protection = Mock(return_value={
            "audit_max_age": 10.0,
            "audit_retry_delay": 0.01,
            "ignore_onboarding_roles": True,
            "grant_punish_at": 3,
            "grant_window": 300.0,
        })
        cog.role_dangerous_permissions = Mock(
            return_value=rules.ROLE_DANGEROUS_PERMISSIONS
        )
        cog.self_assignable_role_ids = Mock(return_value=set())
        cog._resolve_roles = AsyncMock(side_effect=lambda *args: args[-1])
        process_detection = AsyncMock()
        write_log = AsyncMock()
        object.__setattr__(cog, "_process_detection", process_detection)
        object.__setattr__(cog, "write_log", write_log)
        cog.has_tier = Mock(return_value=True)
        cog.role_executor_trusted = Mock(return_value=False)
        cog.state = {"settings": {}}
        cog.punishment = Mock(return_value="ban")
        cog.cfg = lambda guild_id: {"punishments": {}, "modules": {}}
        cog.global_protected_owner_ids = frozenset()
        guild = cast(
            discord.Guild,
            SimpleNamespace(
                id=1,
                owner_id=100,
                get_member=lambda user_id: actors.get(user_id),
            ),
        )
        return cog, guild, process_detection, write_log

    async def test_self_role_selection_is_logged_without_punishment(self):
        member = SimpleNamespace(id=10, bot=False)
        role = make_role(501)
        cog, guild, process_detection, write_log = self.make_cog({10: member})
        entry = make_entry(10, 10, added=[role])
        entry.guild = guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_not_awaited()
        write_log.assert_awaited_once()

    async def test_configured_role_bot_only_bypasses_selectable_member_roles(self):
        role_bot = SimpleNamespace(id=201, bot=True)
        for role_id, role_name in ((501, "GTA"), (502, "Live VC"), (503, "Private VC")):
            with self.subTest(role_name=role_name):
                role = make_role(role_id)
                role.name = role_name
                cog, guild, process_detection, write_log = self.make_cog(
                    {201: role_bot}, trusted_ids=(201,)
                )
                entry = make_entry(201, 10, added=[role])
                entry.guild = guild

                await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

                process_detection.assert_not_awaited()
                write_log.assert_awaited_once()

    async def test_trusted_role_bot_cannot_bypass_dangerous_grant(self):
        role_bot = SimpleNamespace(id=201, bot=True)
        role = make_role(501, administrator=True)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {201: role_bot}, trusted_ids=(201,)
        )
        entry = make_entry(201, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_this_bot_cannot_bypass_dangerous_grant(self):
        own_bot = SimpleNamespace(id=999, bot=True)
        role = make_role(501, administrator=True)
        cog, guild, process_detection, _write_log = self.make_cog({999: own_bot})
        entry = make_entry(999, 10, added=[role])
        entry.guild = guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_untrusted_bot_dangerous_grant_remains_active(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501, administrator=True)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_untrusted_bot_is_not_exempt_from_normal_role_escalation(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_dyno_assigns_single_self_assignable_role_is_exempt(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501)
        cog, _guild, process_detection, write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        cog.self_assignable_role_ids = Mock(return_value={501})
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_not_awaited()
        write_log.assert_awaited_once()

    async def test_dyno_assigns_multiple_self_assignable_roles_is_exempt(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role_a = make_role(501)
        role_b = make_role(502)
        cog, _guild, process_detection, write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        cog.self_assignable_role_ids = Mock(return_value={501, 502})
        entry = make_entry(202, 10, added=[role_a, role_b])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_not_awaited()
        write_log.assert_awaited_once()

    async def test_dyno_assigns_non_self_assignable_role_is_not_exempt(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        cog.self_assignable_role_ids = Mock(return_value={999})
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_dyno_dangerous_grant_still_punished_before_exemption(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501, administrator=True)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        cog.self_assignable_role_ids = Mock(return_value={501})
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_untrusted_bot_ordinary_change_not_auto_trusted(self):
        role_bot = SimpleNamespace(id=202, bot=True)
        role = make_role(501)
        cog, _guild, process_detection, _write_log = self.make_cog(
            {202: role_bot}, trusted_ids=(201,)
        )
        cog.self_assignable_role_ids = Mock(return_value=set())
        entry = make_entry(202, 10, added=[role])
        entry.guild = _guild

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_administrator_role_edit_is_not_implicitly_trusted(self):
        administrator = SimpleNamespace(
            id=300,
            bot=False,
            guild_permissions=discord.Permissions(administrator=True),
        )
        cog, guild, process_detection, _write_log = self.make_cog({300: administrator})
        cog.is_security_admin = Mock(return_value=False)
        cog.is_protected = Mock(return_value=False)
        cog.whitelisted = Mock(return_value=False)
        entry = SimpleNamespace(
            guild=guild,
            user_id=300,
            action=discord.AuditLogAction.role_update,
            target=SimpleNamespace(id=501),
        )
        detection = SimpleNamespace(
            module="antirolepermedit",
            instant=True,
            severity="CRITICAL",
            reason="Dangerous permission change",
        )

        with patch("cogs.security.rules_mod.classify", return_value=detection):
            await cog._handle_audit_entry(cast(discord.AuditLogEntry, entry))

        process_detection.assert_awaited_once()

    async def test_missing_audit_role_diff_is_logged_without_punishment(self):
        cog, guild, process_detection, write_log = self.make_cog({})
        entry = make_entry(20, 10)
        entry.guild = guild
        cog._refetch_role_change = AsyncMock(return_value=([], []))

        await cog._handle_member_role_update(cast(discord.AuditLogEntry, entry))

        process_detection.assert_not_awaited()
        write_log.assert_awaited_once()


class JoinProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixth_join_reaches_raid_counter(self):
        now = 100.0
        cog = SecurityCog.__new__(SecurityCog)
        cog.has_tier = Mock(return_value=True)
        cog.enabled = Mock(side_effect=lambda *args: args[-1] == "antiraid")
        cog.whitelisted = Mock(return_value=False)
        cog._is_alt_account = Mock(return_value=False)
        cog.joins = defaultdict(
            deque,
            {1: deque((now - 5 + offset, offset + 1) for offset in range(5))},
        )
        cog.lockdown_until = {}
        cog.punish = AsyncMock(return_value=(True, "ban", None))
        cog.punishment = Mock(return_value="ban")
        cog.write_log = AsyncMock()
        guild = cast(discord.Guild, SimpleNamespace(id=1))
        member = cast(discord.Member, SimpleNamespace(id=6, bot=False, guild=guild))

        with patch("cogs.security.time.monotonic", return_value=now):
            await cog._handle_member_join(member)

        self.assertIn(1, cog.lockdown_until)
        self.assertEqual(cog.punish.await_count, 6)
        cog.write_log.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()