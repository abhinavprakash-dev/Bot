import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.security import SecurityCog


class AntipromoSlashHandlerTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = SecurityCog.__new__(SecurityCog)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        cog.state = {"settings": {}}
        cog.save = Mock()
        cog.cfg = lambda guild_id: cog.state["settings"].setdefault(str(guild_id), {}).setdefault("security", {}).setdefault("modules", {}) or cog.state["settings"][str(guild_id)]["security"]
        # ensure cfg returns proper structure
        def cfg(guild_id):
            sec = cog.state["settings"].setdefault(str(guild_id), {}).setdefault("security", {})
            sec.setdefault("modules", {})
            sec.setdefault("punishments", {})
            if not isinstance(sec.get("timeouts"), dict):
                sec["timeouts"] = {}
            return sec
        cog.cfg = cfg
        def toggle_module(guild, module, on):
            cog.cfg(guild.id)["modules"][module] = on
            return SimpleNamespace()
        cog.toggle_module = Mock(side_effect=toggle_module)
        cog.punishment = lambda guild_id, module: cog.cfg(guild_id)["punishments"].get(module) or "timeout"
        cog.is_security_admin = Mock(return_value=True)
        return cog

    async def test_state_on_enables(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        # find callback
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state="on", punishment=None)
        self.assertTrue(cog.cfg(1)["modules"].get("antipromo"))

    async def test_state_off_disables(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state="off", punishment=None)
        self.assertFalse(cog.cfg(1)["modules"].get("antipromo", True))

    async def test_punishment_ban_saves(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state=None, punishment="ban")
        self.assertEqual(cog.cfg(1)["punishments"].get("antipromo"), "ban")
        cog.save.assert_called()

    async def test_punishment_kick_saves(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state=None, punishment="kick")
        self.assertEqual(cog.cfg(1)["punishments"].get("antipromo"), "kick")

    async def test_punishment_timeout_saves(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state=None, punishment="timeout")
        self.assertEqual(cog.cfg(1)["punishments"].get("antipromo"), "timeout")

    async def test_omit_punishment_preserves(self):
        cog = self.make_cog()
        cog.cfg(1)["punishments"]["antipromo"] = "kick"
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state=None, punishment=None)
        self.assertEqual(cog.cfg(1)["punishments"].get("antipromo"), "kick")

    async def test_duration_saves(self):
        cog = self.make_cog()
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(send_message=AsyncMock(), is_done=Mock(return_value=False)))
        cmd = cog._make_antipromo_slash("antipromo", "Anti Promotion")
        await cmd.callback(interaction, state=None, punishment=None, duration=30)
        self.assertEqual(cog.cfg(1)["timeouts"].get("antipromo"), 30)
        cog.save.assert_called()

    async def test_duration_default_10(self):
        cog = self.make_cog()
        self.assertEqual(cog.timeout_minutes(1, "antipromo"), 10)

    async def test_default_timeout_when_unset(self):
        cog = self.make_cog()
        self.assertEqual(cog.punishment(1, "antipromo"), "timeout")


if __name__ == "__main__":
    unittest.main()
