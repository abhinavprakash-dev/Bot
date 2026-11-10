import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.community import SelfRoleSelect, SelfRolesCog


class FakeRole:
    def __init__(self):
        self.id = 123
        self.name = "Gamer"
        self.mention = "<@&123>"
        self.managed = False
        self.position = 2
        self.permissions = SimpleNamespace(
            administrator=False,
            manage_guild=False,
            manage_roles=False,
            manage_channels=False,
            manage_webhooks=False,
            ban_members=False,
            kick_members=False,
            moderate_members=False,
            mention_everyone=False,
        )

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class SelfRoleSelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.role = FakeRole()
        self.top_role = SimpleNamespace(position=10)
        self.member = Mock(spec=discord.Member)
        self.member.roles = []
        self.member.add_roles = AsyncMock()
        self.member.remove_roles = AsyncMock()
        self.guild = SimpleNamespace(
            id=456,
            me=SimpleNamespace(top_role=self.top_role),
            get_role=lambda role_id: self.role if role_id == self.role.id else None,
        )
        self.state = {"settings": {"456": {"selfroles": [self.role.id]}}}
        self.cog = SelfRolesCog(SimpleNamespace(user=SimpleNamespace(id=1)), self.state, lambda: None)
        self.client = SimpleNamespace(get_cog=lambda _name: self.cog)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            client=self.client,
            data={"values": [str(self.role.id)]},
        )
        self.component = SelfRoleSelect([
            discord.SelectOption(label=self.role.name, value=str(self.role.id)),
        ])

    async def test_dropdown_selection_adds_role_and_reselection_removes_it(self):
        with patch("cogs.community.ui.respond", new_callable=AsyncMock) as respond:
            await self.component.callback(self.interaction)
            self.assertEqual(self.member.add_roles.await_args.args, (self.role,))
            self.member.roles = [self.role]
            await self.component.callback(self.interaction)

        self.member.remove_roles.assert_awaited_once_with(self.role, reason="Self role")
        self.assertEqual(respond.await_count, 2)

    async def test_dropdown_rejects_roles_removed_from_configuration(self):
        self.state["settings"]["456"]["selfroles"] = []
        with patch("cogs.community.ui.respond", new_callable=AsyncMock) as respond:
            await self.component.callback(self.interaction)

        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        respond.assert_awaited_once()

    async def test_publish_panel_uses_persistent_single_select_and_saves_message(self):
        self.state["settings"]["456"]["selfrole_panel"] = {
            "options": [{"role_id": self.role.id, "label": "Choose Gamer"}],
        }
        message = SimpleNamespace(id=789, channel=SimpleNamespace(id=321), jump_url="https://discord.com/channels/456/321/789")
        channel = SimpleNamespace(id=321, fetch_message=AsyncMock(), send=AsyncMock(return_value=message))
        guild = SimpleNamespace(
            id=456,
            me=SimpleNamespace(top_role=self.top_role),
            get_role=lambda role_id: self.role if role_id == self.role.id else None,
        )
        saved = []
        cog = SelfRolesCog(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            self.state,
            lambda: saved.append(True),
        )

        published = await cog.publish_panel(guild, channel, title="Pick roles", description="Choose one.")

        self.assertIs(published, message)
        channel.send.assert_awaited_once()
        view = channel.send.await_args.kwargs["view"]
        self.assertIsNone(view.timeout)
        self.assertEqual(view.children[0].item.max_values, 1)
        self.assertEqual(view.children[0].item.custom_id, "kryo:selfrole-select")
        self.assertEqual(view.children[0].item.options[0].label, "Choose Gamer")
        self.assertEqual(self.state["settings"]["456"]["selfrole_panel"]["message_id"], 789)
        self.assertEqual(saved, [True])

    async def test_publish_panel_edits_the_existing_bot_message(self):
        channel = SimpleNamespace(id=321, fetch_message=AsyncMock(), send=AsyncMock())
        edited_message = SimpleNamespace(
            id=789,
            channel=channel,
            jump_url="https://discord.com/channels/456/321/789",
        )
        existing_message = SimpleNamespace(
            id=789,
            channel=channel,
            author=SimpleNamespace(id=1),
            edit=AsyncMock(return_value=edited_message),
        )
        channel.fetch_message.return_value = existing_message
        state = {"settings": {"456": {
            "selfroles": [self.role.id],
            "selfrole_panel": {"message_channel_id": 321, "message_id": 789},
        }}}
        cog = SelfRolesCog(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            state,
            lambda: None,
        )
        guild = SimpleNamespace(
            id=456,
            me=SimpleNamespace(top_role=self.top_role),
            get_role=lambda role_id: self.role if role_id == self.role.id else None,
        )

        message = await cog.publish_panel(guild, channel)

        self.assertIs(message, edited_message)
        existing_message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
