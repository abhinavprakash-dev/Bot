import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from botcore import ui
from cogs.tickets import TicketCog, TicketControlsView, TicketPanelSelect, TicketPanelView


class TicketPanelTests(unittest.TestCase):
    def test_panel_uses_categories_configured_for_its_guild(self):
        state = {
            "settings": {
                "123": {
                    "tickets": {
                        "categories": [{
                            "key": "billing",
                            "label": "Billing Help",
                            "description": "Payments and plans",
                            "emoji": "💳",
                            "questions": [],
                        }],
                    },
                },
            },
        }
        cog = TicketCog(SimpleNamespace(), state, lambda: None)

        panel = TicketPanelView(cog, 123)
        select = next(item for item in panel.children if isinstance(item, TicketPanelSelect))

        self.assertEqual(
            [(option.label, option.value, option.description) for option in select.options],
            [("Billing Help", "billing", "Payments and plans")],
        )

    def test_panel_embed_uses_compact_animated_support_icon_and_branding(self):
        state = {"settings": {"123": {"tickets": {
            "panel_title": "Need help?",
            "panel_description": "Choose a support topic.",
            "panel_tagline": "HELP DESK",
            "panel_tagline_description": "Tell us what happened.",
            "panel_next_title": "Next",
            "panel_next_description": "A staff member will reply.",
            "panel_footer": "Powered by our community",
            "panel_color": "#123456",
            "panel_image_url": "https://cdn.example/panel.png",
        }}}}
        cog = TicketCog(SimpleNamespace(user=None), state, lambda: None)
        guild = SimpleNamespace(id=123, name="KRYO Community", icon=None)

        with (
            patch.dict(ui.EMBED_EMOJI_MENTIONS, {"kx_ui_info": "<a:kx_ui_info:123456789012345678>"}),
            patch.dict(ui.EMBED_ICON_URLS, {"kx_ui_info": "https://cdn.example/icon.gif"}),
        ):
            embed = cog.panel_embed(guild)

        self.assertEqual(embed.title, "✧ Need help?")
        self.assertEqual(embed.color.value, 0x123456)
        self.assertEqual(embed.fields[0].name, "<a:kx_ui_info:123456789012345678>  HELP DESK")
        self.assertEqual(embed.fields[0].value, "Tell us what happened.")
        self.assertEqual(embed.fields[1].name, "Next")
        self.assertEqual(embed.fields[1].value, "A staff member will reply.")
        self.assertEqual(embed.footer.text, "Powered by our community")
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/icon.gif")
        self.assertEqual(embed.image.url, "https://cdn.example/panel.png")

    def test_dashboard_payload_lists_only_open_live_ticket_records(self):
        channel = SimpleNamespace(name="billing-ava")
        guild = SimpleNamespace(id=123, get_channel=lambda channel_id: channel if channel_id == 456 else None)
        state = {"settings": {"123": {"tickets": {"records": {
            "456": {
                "status": "open",
                "category_label": "Billing",
                "opener_name": "Ava",
                "created_at": "2026-01-02T03:04:05+00:00",
            },
            "789": {"status": "closed"},
        }}}}}
        cog = TicketCog(SimpleNamespace(), state, lambda: None)

        payload = cog.dashboard_payload(guild)

        self.assertEqual(payload["active_ticket_count"], 1)
        self.assertEqual(payload["active_tickets"], [{
            "channel_id": "456",
            "channel_name": "billing-ava",
            "url": "https://discord.com/channels/123/456",
            "category": "Billing",
            "opener": "Ava",
            "created_at": "2026-01-02T03:04:05+00:00",
            "claimed_by_name": "",
            "assigned_to_name": "",
        }])

    def test_dashboard_payload_recovers_unrecorded_kryo_ticket_channels(self):
        created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        channel = SimpleNamespace(
            id=456,
            name="billing-ava",
            topic="KRYO-TICKET opener:987 category:billing",
            created_at=created_at,
        )
        opener = SimpleNamespace(display_name="Ava")
        guild = SimpleNamespace(
            id=123,
            text_channels=[channel],
            get_channel=lambda channel_id: channel if channel_id == 456 else None,
            get_member=lambda member_id: opener if member_id == 987 else None,
        )
        state = {"settings": {"123": {"tickets": {
            "categories": [{
                "key": "billing",
                "label": "Billing Help",
            }],
            "records": {},
        }}}}
        saved = []
        cog = TicketCog(SimpleNamespace(), state, lambda: saved.append(True))

        payload = cog.dashboard_payload(guild)

        self.assertEqual(payload["active_ticket_count"], 1)
        self.assertEqual(payload["active_tickets"][0]["category"], "Billing Help")
        self.assertEqual(payload["active_tickets"][0]["opener"], "Ava")
        self.assertEqual(payload["active_tickets"][0]["channel_id"], "456")
        self.assertEqual(state["settings"]["123"]["tickets"]["records"]["456"]["opener_id"], 987)
        self.assertEqual(saved, [True])


class TicketPanelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_existing_panel_and_saves_its_message_id(self):
        class FakeTextChannel:
            id = 456

            def permissions_for(self, _member):
                return SimpleNamespace(read_message_history=True)

            async def history(self, limit):
                self.history_limit = limit
                yield message

        message = SimpleNamespace(
            id=789,
            author=SimpleNamespace(id=123),
            components=[SimpleNamespace(children=[SimpleNamespace(custom_id="kryo:tickets:open")])],
        )
        channel = FakeTextChannel()
        guild = SimpleNamespace(
            id=123,
            me=object(),
            get_channel=lambda channel_id: channel if channel_id == 456 else None,
        )
        state = {"settings": {"123": {"tickets": {"panel_channel_id": 456}}}}
        saved = []
        cog = TicketCog(
            SimpleNamespace(user=SimpleNamespace(id=123)),
            state,
            lambda: saved.append(True),
        )

        with patch("cogs.tickets.discord.TextChannel", FakeTextChannel):
            found = await cog.discover_existing_panel(guild)

        self.assertIs(found, message)
        self.assertEqual(channel.history_limit, 100)
        self.assertEqual(state["settings"]["123"]["tickets"]["panel_message_id"], 789)
        self.assertEqual(saved, [True])


class TicketClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_manage_channels_member_without_configured_staff_role_cannot_claim(self):
        member = SimpleNamespace(
            id=456,
            roles=[],
            guild_permissions=SimpleNamespace(manage_channels=True, administrator=False),
        )
        record = {"opener_id": 123, "status": "open", "claimed_by": None}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.interaction_member = AsyncMock(return_value=member)
        cog.get_record = lambda _channel: ({"staff_role_ids": [789]}, record)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            channel=object.__new__(discord.TextChannel),
            response=response,
        )

        await cog.claim(interaction)

        response.send_message.assert_awaited_once_with(
            "Only configured ticket staff can claim this ticket.",
            ephemeral=True,
        )
        self.assertIsNone(record["claimed_by"])

    async def test_configured_ticket_staff_can_claim(self):
        staff_role = SimpleNamespace(id=789)
        member = SimpleNamespace(
            id=456,
            mention="<@456>",
            roles=[staff_role],
            guild_permissions=SimpleNamespace(manage_channels=False, administrator=False),
        )
        record = {"opener_id": 123, "status": "open", "claimed_by": None}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.interaction_member = AsyncMock(return_value=member)
        cog.get_record = lambda _channel: ({"staff_role_ids": [789]}, record)
        saved = []
        cog.save = lambda: saved.append(True)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            channel=object.__new__(discord.TextChannel),
            response=response,
        )

        await cog.claim(interaction)

        self.assertEqual(record["claimed_by"], member.id)
        self.assertEqual(saved, [True])
        response.send_message.assert_awaited_once()


class TicketAssignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_with_manage_channels_cannot_open_assignment_menu(self):
        member = SimpleNamespace(
            id=456,
            roles=[],
            guild_permissions=SimpleNamespace(manage_channels=True, administrator=False),
        )
        cfg = {"staff_role_ids": [789]}
        record = {"opener_id": 123, "status": "open"}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.staff_only = AsyncMock(return_value=(member, cfg, record))
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)

        await cog.start_assignment(interaction)

        response.send_message.assert_awaited_once_with(
            "Only configured ticket staff or server administrators can assign this ticket.",
            ephemeral=True,
        )

    async def test_member_with_manage_channels_cannot_assign_staff(self):
        actor = SimpleNamespace(
            id=456,
            roles=[],
            guild_permissions=SimpleNamespace(manage_channels=True, administrator=False),
        )
        target = SimpleNamespace(id=789)
        cfg = {"staff_role_ids": [999]}
        record = {"opener_id": 123, "status": "open", "assigned_to": None, "claimed_by": None}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.staff_only = AsyncMock(return_value=(actor, cfg, record))
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)

        await cog.assign(interaction, target)

        response.send_message.assert_awaited_once_with(
            "Only configured ticket staff or server administrators can assign this ticket.",
            ephemeral=True,
        )
        self.assertIsNone(record["assigned_to"])
        self.assertIsNone(record["claimed_by"])

    async def test_configured_staff_and_admins_can_use_assignment_permission(self):
        staff_role = SimpleNamespace(id=789)
        staff_member = SimpleNamespace(
            roles=[staff_role],
            guild_permissions=SimpleNamespace(manage_channels=False, administrator=False),
        )
        admin_member = SimpleNamespace(
            roles=[],
            guild_permissions=SimpleNamespace(manage_channels=False, administrator=True),
        )
        cfg = {"staff_role_ids": [staff_role.id]}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)

        self.assertTrue(cog.is_ticket_staff_or_admin(staff_member, cfg))
        self.assertTrue(cog.is_ticket_staff_or_admin(admin_member, cfg))


class TicketTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcript_is_readable_plain_text_not_html(self):
        message = SimpleNamespace(
            created_at=datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
            author=SimpleNamespace(display_name="Support Agent"),
            clean_content="Thanks for contacting support.\nWe can help with this.",
            attachments=[SimpleNamespace(filename="receipt.png", url="https://cdn.example.com/receipt.png")],
        )

        class TranscriptChannel:
            name = "general-c-r-u-z"

            def history(self, limit, oldest_first):
                self.history_options = (limit, oldest_first)

                async def messages():
                    yield message

                return messages()

        channel = TranscriptChannel()
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        transcript = await cog.make_transcript(channel, {"category_label": "General Support"})
        content = transcript.getvalue().decode("utf-8")

        self.assertEqual(channel.history_options, (500, True))
        self.assertIn("KRYO SUPPORT TRANSCRIPT — general-c-r-u-z", content)
        self.assertIn("Category: General Support", content)
        self.assertIn("[2026-10-16 12:00 UTC] Support Agent", content)
        self.assertIn("Thanks for contacting support.\nWe can help with this.", content)
        self.assertIn("Attachment: receipt.png — https://cdn.example.com/receipt.png", content)
        self.assertNotIn("<html", content.lower())
        self.assertNotIn("<style", content.lower())


class TicketDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def run_delete(self, *, is_staff=False, status="closed"):
        class FakeChannel:
            id = 987

            def __init__(self):
                self.delete = AsyncMock()

        channel = FakeChannel()
        member = SimpleNamespace(id=123, mention="<@123>")
        record = {"opener_id": 456, "status": status}
        cfg = {"records": {str(channel.id): record}, "staff_role_ids": []}
        saved = []
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.interaction_member = AsyncMock(return_value=member)
        cog.get_record = lambda _channel: (cfg, record)
        cog.is_staff = lambda _member, _cfg: is_staff
        cog.save = lambda: saved.append(True)
        response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            channel=channel,
            response=response,
            followup=followup,
        )

        with patch("cogs.tickets.discord.TextChannel", FakeChannel):
            await cog.delete_ticket(interaction)
        return channel, record, cfg, saved, response, followup

    async def test_non_staff_member_cannot_delete_closed_ticket(self):
        channel, record, cfg, saved, response, followup = await self.run_delete()

        channel.delete.assert_not_awaited()
        self.assertIn(str(channel.id), cfg["records"])
        self.assertEqual(saved, [])
        response.defer.assert_not_awaited()
        response.send_message.assert_awaited_once_with(
            "Only configured ticket staff or server administrators can delete this ticket.",
            ephemeral=True,
        )

    async def test_ticket_staff_can_delete_closed_ticket(self):
        channel, record, cfg, saved, response, followup = await self.run_delete(is_staff=True)

        channel.delete.assert_awaited_once()
        self.assertNotIn(str(channel.id), cfg["records"])
        self.assertEqual(saved, [True])
        response.defer.assert_awaited_once()
        followup.send.assert_awaited_once()

    async def test_ticket_staff_must_close_ticket_before_deleting(self):
        channel, record, cfg, saved, response, followup = await self.run_delete(is_staff=True, status="open")

        channel.delete.assert_not_awaited()
        self.assertIn(str(channel.id), cfg["records"])
        self.assertEqual(saved, [])
        response.send_message.assert_awaited_once_with(
            "Close the ticket before deleting its channel.",
            ephemeral=True,
        )

    async def test_ticket_opener_cannot_close_their_own_ticket(self):
        class FakeChannel:
            guild = object()

        channel = FakeChannel()
        opener = SimpleNamespace(id=123)
        record = {"opener_id": opener.id, "status": "open"}
        cfg = {"staff_role_ids": []}
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        cog.interaction_member = AsyncMock(return_value=opener)
        cog.get_record = lambda _channel: (cfg, record)
        cog.is_staff = lambda _member, _cfg: False
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(channel=channel, response=response)

        with patch("cogs.tickets.discord.TextChannel", FakeChannel):
            await cog.close_ticket(interaction)

        response.send_message.assert_awaited_once_with(
            "Only configured ticket staff or server administrators can close this ticket.",
            ephemeral=True,
        )
        self.assertEqual(record["status"], "open")

    def test_open_ticket_controls_include_empty_ticket_delete_action(self):
        view = TicketControlsView(TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None))
        custom_ids = [item.custom_id for item in view.children]

        self.assertIn("kryo:tickets:delete", custom_ids)
        self.assertNotIn("kryo:tickets:add-member", custom_ids)
        self.assertNotIn("kryo:tickets:remove-member", custom_ids)

    async def test_legacy_add_member_action_is_rejected(self):
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)

        await cog.reject_extra_member_access(interaction)

        response.send_message.assert_awaited_once_with(
            "Tickets are private to the opener and configured support staff. Additional members cannot be added.",
            ephemeral=True,
        )


class TicketPrivacyTests(unittest.IsolatedAsyncioTestCase):
    class FakeRole:
        def __init__(self, role_id, default=False):
            self.id = role_id
            self.default = default

        def is_default(self):
            return self.default

        def __hash__(self):
            return hash(self.id)

    class FakeMember:
        def __init__(self, member_id):
            self.id = member_id

        def __hash__(self):
            return hash(self.id)

    def make_guild(self):
        everyone = self.FakeRole(1, default=True)
        ordinary_role = self.FakeRole(2)
        staff_role = self.FakeRole(3)
        bot_member = self.FakeMember(10)
        opener = self.FakeMember(11)
        guild = SimpleNamespace(
            default_role=everyone,
            roles=[everyone, ordinary_role, staff_role],
            me=bot_member,
            get_member=lambda member_id: opener if member_id == opener.id else None,
        )
        return guild, everyone, ordinary_role, staff_role, bot_member, opener

    async def test_channel_overwrites_deny_all_non_staff_roles(self):
        guild, everyone, ordinary_role, staff_role, bot_member, opener = self.make_guild()

        overwrites = TicketCog.ticket_overwrites(guild, opener, {"staff_role_ids": [staff_role.id]})

        self.assertFalse(overwrites[everyone].view_channel)
        self.assertFalse(overwrites[ordinary_role].view_channel)
        self.assertTrue(overwrites[staff_role].view_channel)
        self.assertTrue(overwrites[opener].view_channel)
        self.assertTrue(overwrites[bot_member].view_channel)

    async def test_existing_ticket_removes_other_member_and_role_access(self):
        guild, everyone, ordinary_role, staff_role, bot_member, opener = self.make_guild()
        other_member = self.FakeMember(12)
        existing_overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            ordinary_role: discord.PermissionOverwrite(view_channel=True),
            other_member: discord.PermissionOverwrite(view_channel=True),
        }
        channel = SimpleNamespace(
            id=900,
            guild=guild,
            overwrites=existing_overwrites,
            edit=AsyncMock(),
        )
        cog = TicketCog(SimpleNamespace(), {"settings": {}}, lambda: None)

        with patch("cogs.tickets.discord.Role", self.FakeRole):
            await cog.secure_existing_ticket(
                channel,
                {"opener_id": opener.id},
                {"staff_role_ids": [staff_role.id]},
            )

        channel.edit.assert_awaited_once()
        secured = channel.edit.await_args.kwargs["overwrites"]
        self.assertNotIn(other_member, secured)
        self.assertFalse(secured[ordinary_role].view_channel)
        self.assertTrue(secured[staff_role].view_channel)
        self.assertTrue(secured[opener].view_channel)
        self.assertTrue(secured[bot_member].view_channel)


if __name__ == "__main__":
    unittest.main()
