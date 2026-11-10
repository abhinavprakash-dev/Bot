import asyncio
import unittest
from types import SimpleNamespace
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands

from cogs.vcmove import (
    VCMoveCog,
    _can_move,
    _can_move_slash,
    _move_members_in_batches,
)


class VCMoveCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_move_permission_checks_handle_missing_guilds(self):
        context = cast(commands.Context, SimpleNamespace(guild=None, author=object()))
        interaction = cast(
            discord.Interaction,
            SimpleNamespace(guild=None, user=object()),
        )

        prefix_error = await _can_move(context)
        slash_error = await _can_move_slash(interaction)

        self.assertIsNotNone(prefix_error)
        self.assertIsNotNone(slash_error)

    def test_drag_all_slash_command_is_registered_with_permission_checks(self):
        cog = VCMoveCog(bot=cast(commands.Bot, object()))
        commands_by_name = {command.name: command for command in cog.get_app_commands()}

        self.assertIn("drag", commands_by_name)
        drag = commands_by_name["drag"]
        self.assertIsInstance(drag, app_commands.Group)
        self.assertEqual([command.name for command in drag.commands], ["all"])

        drag_all = drag.get_command("all")
        self.assertIsNotNone(drag_all)
        self.assertTrue(drag_all.checks)
        self.assertEqual(
            [(parameter.name, parameter.required) for parameter in drag_all.parameters],
            [("dest", True), ("source", False)],
        )

    async def test_moveall_moves_members_in_bounded_concurrent_batches(self):
        active_moves = 0
        maximum_concurrent_moves = 0

        class FakeMember:
            async def move_to(self, _destination, *, reason):
                nonlocal active_moves, maximum_concurrent_moves
                active_moves += 1
                maximum_concurrent_moves = max(maximum_concurrent_moves, active_moves)
                await asyncio.sleep(0.01)
                active_moves -= 1

        members = cast(list[discord.Member], [FakeMember() for _ in range(12)])

        moved, failed = await _move_members_in_batches(
            members,
            cast(discord.VoiceChannel, object()),
            "test",
        )

        self.assertEqual((moved, failed), (12, 0))
        self.assertEqual(maximum_concurrent_moves, 5)


if __name__ == "__main__":
    unittest.main()
