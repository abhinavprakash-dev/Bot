import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.security import SecurityCog


class FakeAuditEntry:
    def __init__(self, target_id, user_id, created_at=None):
        self.target = SimpleNamespace(id=target_id)
        self.user_id = user_id
        self.created_at = created_at or datetime.now(timezone.utc)


class FakeAuditLog:
    def __init__(self, entries):
        self._entries = entries

    async def __aiter__(self):
        for entry in self._entries:
            yield entry


class BotJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(
            id=1,
            name="KRYO Test Server",
            owner_id=100,
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)),
            get_member=lambda uid: self._members.get(uid),
            kick=AsyncMock(),
        )
        self._members = {}
        self.bot_user = SimpleNamespace(id=999)
        self.cog = SecurityCog.__new__(SecurityCog)
        self.cog.bot = SimpleNamespace(user=self.bot_user)
        self.cog.enabled = lambda guild_id, module: True
        self.cog.is_security_admin = lambda guild, member: getattr(member, "_secadmin", False)
        self.cog.guild_settings = lambda guild_id: {"security_admin_role_id": 500}
        self.cog.save = Mock()

        secadmin = SimpleNamespace(
            id=200,
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[SimpleNamespace(id=500)],
            _secadmin=True,
        )
        self._members[200] = secadmin

        normal_admin = SimpleNamespace(
            id=300,
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
            _secadmin=False,
        )
        self._members[300] = normal_admin

        ordinary = SimpleNamespace(
            id=400,
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
            _secadmin=False,
        )
        self._members[400] = ordinary

    def _make_bot_member(self, bot_id=5000):
        return SimpleNamespace(id=bot_id, bot=True, guild=self.guild)

    async def test_owner_adds_bot_not_kicked(self):
        bot_member = self._make_bot_member()
        entry = FakeAuditEntry(bot_member.id, 100, datetime.now(timezone.utc))
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_not_awaited()

    async def test_secadmin_adds_bot_not_kicked(self):
        bot_member = self._make_bot_member()
        entry = FakeAuditEntry(bot_member.id, 200, datetime.now(timezone.utc))
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_not_awaited()

    async def test_normal_admin_adds_bot_kicked(self):
        bot_member = self._make_bot_member()
        entry = FakeAuditEntry(bot_member.id, 300, datetime.now(timezone.utc))
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_awaited_once()

    async def test_ordinary_member_adds_bot_kicked(self):
        bot_member = self._make_bot_member()
        entry = FakeAuditEntry(bot_member.id, 400, datetime.now(timezone.utc))
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_awaited_once()

    async def test_audit_entry_different_bot_not_authorized(self):
        bot_member = self._make_bot_member(5000)
        entry = FakeAuditEntry(9999, 100, datetime.now(timezone.utc))
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_awaited_once()

    async def test_stale_audit_entry_not_authorized(self):
        bot_member = self._make_bot_member()
        stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        entry = FakeAuditEntry(bot_member.id, 100, stale_time)
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([entry])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_awaited_once()

    async def test_no_audit_entry_kicks_bot(self):
        bot_member = self._make_bot_member()
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_awaited_once()

    async def test_disabled_antibot_does_not_kick(self):
        self.cog.enabled = lambda guild_id, module: False
        bot_member = self._make_bot_member()
        self.guild.audit_logs = lambda limit=10, action=None: FakeAuditLog([])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.cog._handle_bot_join(bot_member)
        self.guild.kick.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
