import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import discord

from botcore.security import lockdown, snapshots
from cogs.security import SecurityCog


class FakeChannel:
    def __init__(self, channel_id, name, default_role):
        self.id = channel_id
        self.name = name
        self._overwrites = {default_role: discord.PermissionOverwrite()}

    def overwrites_for(self, target):
        return self._overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, *, overwrite, reason):
        self._overwrites[target] = overwrite


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.default_role = discord.Object(id=self.id)
        self.channels = [FakeChannel(10, "general", self.default_role)]
        self.default_channel = self.channels[0]
        self.system_channel = None
        self.me = SimpleNamespace(guild_permissions=discord.Permissions(administrator=True))

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    def get_member(self, user_id):
        return None


class LockdownTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, state=None, clock=lambda: 100):
        persisted = state if state is not None else {}
        manager = lockdown.LockdownManager(
            state_for=lambda guild_id: persisted.setdefault(str(guild_id), {}),
            save=lambda: None,
            clock=clock,
        )
        return manager, persisted

    async def test_lockdown_blocks_the_default_channel_and_restores_overwrites(self):
        guild = FakeGuild()
        manager, _ = self.make_manager()

        result = await manager.engage(guild)

        self.assertEqual(len(result.state.touched), 1)
        self.assertFalse(guild.channels[0].overwrites_for(guild.default_role).send_messages)
        await manager.release(guild)
        self.assertIsNone(guild.channels[0].overwrites_for(guild.default_role).send_messages)

    async def test_expired_persisted_lockdown_can_be_released_after_restart(self):
        guild = FakeGuild()
        now = [100]
        manager, persisted = self.make_manager(clock=lambda: now[0])
        await manager.engage(guild, duration=60)
        now[0] += 61
        restarted, _ = self.make_manager(persisted, clock=lambda: now[0])

        state = restarted.restore_on_startup(cast(discord.Guild, guild))
        result = await lockdown.sweep_expired(restarted, guild)

        self.assertIsNotNone(state)
        self.assertIsNotNone(result)
        self.assertIsNone(guild.channels[0].overwrites_for(guild.default_role).send_messages)


class SnapshotRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_all_passes_report_and_reason_to_settings_restore(self):
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(guild_permissions=discord.Permissions(administrator=True)),
        )
        with (
            patch("botcore.security.snapshots.restore_roles", new_callable=AsyncMock),
            patch("botcore.security.snapshots.restore_channels", new_callable=AsyncMock),
            patch("botcore.security.snapshots.restore_settings", new_callable=AsyncMock) as restore_settings,
            patch("botcore.security.snapshots.restore_icon", new_callable=AsyncMock),
        ):
            report = await snapshots.restore_all(
                cast(discord.Guild, guild),
                {"settings": {"name": "Saved name"}},
                "panic restore",
            )

        args = restore_settings.await_args.args
        self.assertIs(args[2], report)
        self.assertEqual(args[3], "Saved name")


class PanicTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_and_lockdown_slash_commands_defer_before_work(self):
        command_cases = (
            (SecurityCog.backup_slash, "do_backup", ("before-raid",)),
            (SecurityCog.restore_slash, "do_restore", (None,)),
            (
                SecurityCog.lockdown_slash,
                "do_lockdown",
                (discord.app_commands.Choice(name="Lock", value="on"), 15),
            ),
        )
        for command, method_name, arguments in command_cases:
            with self.subTest(command=command.name):
                calls = []
                guild = SimpleNamespace(id=1)
                interaction = SimpleNamespace(
                    guild=guild,
                    response=SimpleNamespace(
                        defer=AsyncMock(side_effect=lambda **kwargs: calls.append("defer"))
                    ),
                    edit_original_response=AsyncMock(
                        side_effect=lambda **kwargs: calls.append("edit")
                    ),
                )
                cog = SecurityCog.__new__(SecurityCog)

                async def perform_work(*args):
                    calls.append("work")
                    return discord.Embed(title="Done")

                setattr(cog, method_name, perform_work)

                await command.callback(cog, interaction, *arguments)

                self.assertEqual(calls, ["defer", "work", "edit"])

    async def test_panic_slash_defers_before_running_long_restore(self):
        calls = []
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(
            guild=guild,
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=lambda **kwargs: calls.append("defer"))
            ),
            edit_original_response=AsyncMock(
                side_effect=lambda **kwargs: calls.append("edit")
            ),
        )
        cog = SecurityCog.__new__(SecurityCog)

        async def do_panic(target_guild):
            calls.append("panic")
            return discord.Embed(title="Panic handled")

        cog.do_panic = do_panic

        await SecurityCog.panic_slash.callback(cog, interaction)

        self.assertEqual(calls, ["defer", "panic", "edit"])
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.edit_original_response.assert_awaited_once()

    async def test_panic_restores_snapshot_before_reapplying_lockdown(self):
        operations = []
        cog = SecurityCog.__new__(SecurityCog)
        cog.has_tier = lambda guild_id, tier: True
        cog.snapshots = SimpleNamespace(latest=lambda guild_id: {"created_at": 1})

        async def engage(*args, **kwargs):
            operations.append("lock")
            return SimpleNamespace(
                state=SimpleNamespace(touched=[1]),
                notes=[],
            )

        cog.lockdowns = SimpleNamespace(engage=AsyncMock(side_effect=engage))

        async def restore_all(*args, **kwargs):
            operations.append("restore")
            return SimpleNamespace(summary=lambda: "restored", lines=lambda: "done")

        with patch("cogs.security.snap_mod.restore_all", side_effect=restore_all):
            await cog.do_panic(cast(discord.Guild, SimpleNamespace(id=1)))

        self.assertEqual(operations, ["restore", "lock"])


if __name__ == "__main__":
    unittest.main()
