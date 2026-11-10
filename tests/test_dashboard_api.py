import unittest
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from aiohttp.web import Request
from discord.ext.commands import Bot

from botcore import ui
from botcore.dashboard_api import (
    DashboardAPI,
    DashboardConfig,
    DashboardSession,
    GUILD_ACCESS_CACHE_SECONDS,
    OAUTH_STATE_COOKIE,
    SESSION_SECONDS,
)
from cogs.community import WelcomerCog
from cogs.jail import JailCog
from cogs.security import SecurityCog


class DashboardAPISettingsTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self, state, prime=False):
        api = object.__new__(DashboardAPI)
        api.state = state
        api.bot = SimpleNamespace(get_cog=lambda _name: None)
        api.save = lambda: None
        api.has_tier = lambda _guild_id, _tier: prime
        api.authorized_guild = AsyncMock(return_value=(object(), SimpleNamespace(id=123)))
        api.settings_payload = lambda _guild, _user_id=None: {"whitelist": {"users": []}}
        return api

    @staticmethod
    def request(payload):
        async def json():
            return payload

        return SimpleNamespace(json=json, match_info={"guild_id": "123"})

    def test_settings_payload_includes_guild_and_bot_branding(self):
        api = object.__new__(DashboardAPI)
        api.state = {}
        ticket_cog = SimpleNamespace(dashboard_payload=lambda _guild: {
            "active_ticket_count": 1,
            "active_tickets": [{"channel_id": "456", "channel_name": "support-ava"}],
        })
        api.bot = SimpleNamespace(
            user=SimpleNamespace(
                name="KRYO",
                display_avatar=SimpleNamespace(url="https://cdn.example.com/bot.png"),
            ),
            get_cog=lambda name: ticket_cog if name == "TicketCog" else None,
        )
        api.has_tier = lambda _guild_id, _tier: False
        guild = SimpleNamespace(
            id=123,
            name="Demo",
            icon=SimpleNamespace(url="https://cdn.example.com/server.png"),
            text_channels=[],
            roles=[],
            me=None,
        )

        payload = api.settings_payload(guild)

        self.assertEqual(
            payload["guild"],
            {"id": "123", "name": "Demo", "icon": "https://cdn.example.com/server.png"},
        )
        self.assertEqual(
            payload["bot"],
            {"name": "KRYO", "avatar": "https://cdn.example.com/bot.png"},
        )
        self.assertEqual(payload["jail"], {
            "role_id": "",
            "role_name": "",
            "members": [],
            "manage_roles_available": False,
        })
        self.assertEqual(payload["ticketing"]["active_ticket_count"], 1)
        self.assertEqual(payload["ticketing"]["active_tickets"], [{
            "channel_id": "456",
            "channel_name": "support-ava",
        }])

    def test_settings_payload_includes_whitelisted_role_names(self):
        role = SimpleNamespace(id=456, name="Trusted Staff", is_default=lambda: False)
        api = object.__new__(DashboardAPI)
        api.state = {"settings": {"123": {"antinuke_whitelist_roles": [456, 789]}}}
        api.bot = SimpleNamespace(user=None)
        api.has_tier = lambda _guild_id, _tier: False
        guild = SimpleNamespace(
            id=123,
            name="Demo",
            icon=None,
            text_channels=[],
            roles=[role],
            me=None,
        )

        payload = api.settings_payload(guild)

        self.assertEqual(payload["whitelist"]["roles"], ["456", "789"])
        self.assertEqual(payload["whitelist"]["role_names"], {"456": "Trusted Staff"})

    def test_settings_payload_includes_jailed_member_names_and_role(self):
        role = SimpleNamespace(id=555, name="KRYO Jail")
        member = SimpleNamespace(display_name="TestMember")
        api = object.__new__(DashboardAPI)
        api.state = {"settings": {"123": {"jail": {
            "role_id": 555,
            "members": {"789": {
                "name": "Saved name",
                "jailed_at": "2026-10-16T12:00:00+00:00",
                "reason": "Review",
            }},
        }}}}
        api.bot = SimpleNamespace(user=None)
        api.has_tier = lambda _guild_id, _tier: False
        guild = SimpleNamespace(
            id=123, name="Demo", icon=None, text_channels=[], roles=[], me=None,
            get_role=lambda role_id: role if role_id == role.id else None,
            get_member=lambda member_id: member if member_id == 789 else None,
        )

        payload = api.settings_payload(guild)

        self.assertEqual(payload["jail"]["role_id"], "555")
        self.assertEqual(payload["jail"]["role_name"], "KRYO Jail")
        self.assertEqual(payload["jail"]["members"], [{
            "id": "789",
            "name": "TestMember",
            "jailed_at": "2026-10-16T12:00:00+00:00",
            "reason": "Review",
        }])

    def test_settings_payload_includes_all_roles_below_the_bot(self):
        class Role:
            def __init__(self, role_id, name, position, *, managed=False, administrator=False, default=False):
                self.id = role_id
                self.name = name
                self.position = position
                self.managed = managed
                self.default = default
                self.permissions = SimpleNamespace(
                    administrator=administrator,
                    manage_guild=False,
                    manage_roles=False,
                    manage_channels=False,
                    manage_webhooks=False,
                    ban_members=False,
                    kick_members=False,
                    moderate_members=False,
                    mention_everyone=False,
                )

            def is_default(self):
                return self.default

            def __lt__(self, other):
                return self.position < other.position

        bot_role = Role(999, "KRYO", 10)
        everyone = Role(123, "@everyone", 0, default=True)
        ordinary = Role(124, "Members", 2)
        dangerous_role = Role(125, "Administrator", 3, administrator=True)
        managed_role = Role(126, "Integration", 4, managed=True)
        jail_role = Role(127, "KRYO Jail", 5)
        above_bot = Role(128, "Owner Role", 11)
        roles = [everyone, ordinary, dangerous_role, managed_role, jail_role, above_bot]
        api = object.__new__(DashboardAPI)
        api.state = {"settings": {"123": {
            "jail": {"role_id": jail_role.id},
            "selfroles": [ordinary.id],
            "selfrole_panel": {
                "channel_id": 456,
                "title": "Pick your roles",
                "description": "Choose a role.",
            },
        }}}
        api.bot = SimpleNamespace(user=None, guilds=[])
        api.has_tier = lambda _guild_id, _tier: False
        guild = SimpleNamespace(
            id=123,
            name="Demo",
            icon=None,
            text_channels=[],
            roles=roles,
            me=SimpleNamespace(
                top_role=bot_role,
                guild_permissions=SimpleNamespace(
                    manage_roles=True,
                    manage_channels=False,
                    manage_guild=False,
                ),
            ),
            get_role=lambda role_id: next((role for role in roles if role.id == role_id), None),
            get_member=lambda _member_id: None,
        )

        payload = api.settings_payload(guild)

        self.assertEqual(
            [role["id"] for role in payload["roles"]],
            ["124", "125", "126", "127"],
        )
        self.assertTrue(next(role for role in payload["roles"] if role["id"] == "125")["dangerous"])
        self.assertTrue(next(role for role in payload["roles"] if role["id"] == "126")["managed"])
        self.assertTrue(next(role for role in payload["roles"] if role["id"] == "127")["jail_role"])
        self.assertEqual(payload["self_roles"], {
            "role_ids": ["124"],
            "options": [{"role_id": "124", "label": "Members"}],
            "channel_id": "456",
            "title": "Pick your roles",
            "description": "Choose a role.",
        })

    async def test_dashboard_saves_valid_multi_autoroles_and_self_role_panel_settings(self):
        class Role:
            id = 456
            managed = False
            permissions = SimpleNamespace(
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

            def __ge__(self, _other):
                return False

        role = Role()
        channel = SimpleNamespace(id=789)
        bot_member = SimpleNamespace(
            top_role=object(),
            guild_permissions=SimpleNamespace(manage_roles=True),
        )
        guild = SimpleNamespace(
            id=123,
            me=bot_member,
            get_role=lambda role_id: role if role_id == role.id else None,
        )
        state = {"settings": {"123": {}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(object(), guild))
        saved = []
        api.save = lambda: saved.append(True)

        invalid_response = await api.update_settings(self.request({
            "self_roles": {"options": [{"role_id": "456", "label": " "}]},
        }))
        self.assertEqual(invalid_response.status, 400)
        self.assertEqual(state["settings"]["123"], {})
        self.assertEqual(saved, [])

        with patch.object(DashboardAPI, "text_channel", return_value=channel):
            response = await api.update_settings(self.request({
                "autorole": {"human": ["456"], "bot": []},
                "self_roles": {
                    "options": [{"role_id": "456", "label": "Players"}],
                    "channel_id": "789",
                    "title": "Choose your roles",
                    "description": "Select roles to add or remove them.",
                },
            }))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["autorole"], {"human": [456], "bot": []})
        self.assertEqual(state["settings"]["123"]["selfroles"], [456])
        self.assertEqual(state["settings"]["123"]["selfrole_panel"], {
            "channel_id": 789,
            "title": "Choose your roles",
            "description": "Select roles to add or remove them.",
            "options": [{"role_id": 456, "label": "Players"}],
        })
        self.assertEqual(saved, [True])

    async def test_dashboard_rejects_dangerous_self_role_without_saving_changes(self):
        class Role:
            id = 456
            managed = False
            permissions = SimpleNamespace(
                administrator=True,
                manage_guild=False,
                manage_roles=False,
                manage_channels=False,
                manage_webhooks=False,
                ban_members=False,
                kick_members=False,
                moderate_members=False,
                mention_everyone=False,
            )

            def __ge__(self, _other):
                return False

        role = Role()
        guild = SimpleNamespace(
            id=123,
            me=SimpleNamespace(
                top_role=object(),
                guild_permissions=SimpleNamespace(manage_roles=True),
            ),
            get_role=lambda role_id: role if role_id == role.id else None,
        )
        state = {"settings": {"123": {"selfroles": [999]}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(object(), guild))
        saved = []
        api.save = lambda: saved.append(True)

        response = await api.update_settings(self.request({
            "self_roles": {"role_ids": ["456"]},
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["selfroles"], [999])
        self.assertEqual(saved, [])

    async def test_dashboard_publishes_self_role_panel_to_configured_channel(self):
        channel = SimpleNamespace(
            id=789,
            permissions_for=lambda _member: SimpleNamespace(
                send_messages=True,
                embed_links=True,
            ),
        )
        message = SimpleNamespace(
            id=987,
            channel=channel,
            jump_url="https://discord.com/channels/123/789/987",
        )
        self_roles_cog = SimpleNamespace(publish_panel=AsyncMock(return_value=message))
        guild = SimpleNamespace(
            id=123,
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)),
        )
        api = self.make_api({"settings": {"123": {"selfrole_panel": {
            "channel_id": 789,
            "title": "Choose your roles",
            "description": "Choose roles.",
        }}}})
        api.authorized_guild = AsyncMock(return_value=(object(), guild))
        api.bot = SimpleNamespace(get_cog=lambda name: self_roles_cog if name == "Self Roles" else None)

        with patch.object(DashboardAPI, "text_channel", return_value=channel):
            response = await api.publish_self_roles_panel(self.request({}))

        self.assertEqual(response.status, 200)
        self_roles_cog.publish_panel.assert_awaited_once_with(
            guild,
            channel,
            title="Choose your roles",
            description="Choose roles.",
        )
        self.assertIn('"message_id": "987"', response.text)

    async def test_rejected_update_does_not_partially_change_settings(self):
        original = {"settings": {"123": {"welcome": {"message": "Existing message"}}}}
        api = self.make_api(original, prime=True)

        response = await api.update_settings(self.request({
            "welcome": {"message": "Would be a partial update"},
            "logs": {"channels": {"unknown": "456"}},
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(
            original["settings"]["123"]["welcome"]["message"],
            "Existing message",
        )

    async def test_free_server_can_disable_existing_protection(self):
        state = {"settings": {"123": {"security": {"modules": {"antiraid": True}}}}}
        api = self.make_api(state)
        api.whitelist_user_names = AsyncMock()
        saved = []
        api.save = lambda: saved.append(True)

        response = await api.update_settings(self.request({"modules": {"antiraid": False}}))

        self.assertEqual(response.status, 200)
        self.assertFalse(state["settings"]["123"]["security"]["modules"]["antiraid"])
        self.assertFalse(state["settings"]["123"]["antinuke_enabled"])
        self.assertEqual(saved, [True])
        api.whitelist_user_names.assert_not_awaited()

    async def test_protection_timeout_duration_is_saved(self):
        state = {"settings": {"123": {"security": {}}}}
        api = self.make_api(state, prime=True)

        response = await api.update_settings(self.request({
            "punishments": {"antichannel": "timeout"},
            "timeouts": {"antichannel": 90},
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["security"]["timeouts"]["antichannel"], 90)

    async def test_timeout_duration_requires_timeout_punishment_and_valid_minutes(self):
        for payload in (
            {"timeouts": {"antichannel": 60}},
            {"punishments": {"antichannel": "timeout"}, "timeouts": {"antichannel": 0}},
            {"punishments": {"antichannel": "timeout"}, "timeouts": {"antichannel": 40321}},
            {"punishments": {"antilink": "timeout"}, "timeouts": {"antilink": 60}},
        ):
            with self.subTest(payload=payload):
                state = {"settings": {"123": {"security": {"timeouts": {"antichannel": 30}}}}}
                api = self.make_api(state, prime=True)
                response = await api.update_settings(self.request(payload))
                self.assertEqual(response.status, 400)
                self.assertEqual(state["settings"]["123"]["security"]["timeouts"], {"antichannel": 30})

    async def test_dashboard_jail_action_calls_jail_cog(self):
        api = self.make_api({"settings": {}})
        member_id = 12345678901234567
        jail_cog = SimpleNamespace(jail_member=AsyncMock())
        api.bot = SimpleNamespace(get_cog=lambda name: jail_cog if name == "JailCog" else None)
        request = self.request({
            "action": "jail",
            "member_id": str(member_id),
            "reason": "Review report",
        })

        response = await api.manage_jail(request)

        self.assertEqual(response.status, 200)
        jail_cog.jail_member.assert_awaited_once_with(
            SimpleNamespace(id=123),
            member_id,
            "Review report",
            "Dashboard moderator",
        )

    async def test_dashboard_jail_rejects_invalid_member_id(self):
        api = self.make_api({"settings": {}})
        jail_cog = SimpleNamespace(jail_member=AsyncMock())
        api.bot = SimpleNamespace(get_cog=lambda _name: jail_cog)

        response = await api.manage_jail(self.request({
            "action": "jail",
            "member_id": "",
        }))

        self.assertEqual(response.status, 400)
        jail_cog.jail_member.assert_not_awaited()

    async def test_dashboard_jail_accepts_username(self):
        api = self.make_api({"settings": {}})
        jail_cog = SimpleNamespace(jail_member=AsyncMock())
        api.bot = SimpleNamespace(get_cog=lambda _name: jail_cog)

        response = await api.manage_jail(self.request({
            "action": "jail",
            "member_id": "ExactUsername",
        }))

        self.assertEqual(response.status, 200)
        jail_cog.jail_member.assert_awaited_once_with(
            SimpleNamespace(id=123),
            "ExactUsername",
            "",
            "Dashboard moderator",
        )

    async def test_whitelist_update_resolves_username_to_member_id(self):
        member = SimpleNamespace(
            id=12345678901234567,
            name="ExactUsername",
            display_name="ExactUsername",
            global_name=None,
        )
        guild = SimpleNamespace(
            id=123,
            members=[member],
            get_member=lambda member_id: member if member_id == member.id else None,
        )
        state = {"settings": {"123": {}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(object(), guild))
        jail_cog = JailCog(SimpleNamespace(), state, lambda: None)
        api.bot = SimpleNamespace(
            get_cog=lambda name: jail_cog if name == "JailCog" else None,
        )
        api.whitelist_user_names = AsyncMock(return_value={str(member.id): member.name})

        response = await api.update_settings(self.request({
            "whitelist": {"users": ["ExactUsername"]},
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["antinuke_whitelist"], [member.id])

    async def test_settings_payload_includes_existing_and_default_timeout_lengths(self):
        api = object.__new__(DashboardAPI)
        api.state = {"settings": {"123": {
            "security": {"timeouts": {"antichannel": 90}},
            "welcome": {
                "style": "image_card",
                "font_size": 48,
                "title_font_size": 60,
                "video_url": "https://youtu.be/example",
                "dm_enabled": True,
            },
        }}}
        api.bot = SimpleNamespace(user=None)
        api.has_tier = lambda _guild_id, _tier: True
        guild = SimpleNamespace(
            id=123,
            name="Test server",
            icon=None,
            roles=[],
            text_channels=[],
            emojis=[SimpleNamespace(name="kenji_crown_3", id=456, animated=False)],
            me=None,
        )
        other_guild = SimpleNamespace(
            id=456,
            emojis=[SimpleNamespace(name="shared_emoji", id=789, animated=True)],
        )
        api.bot.guilds = [guild, other_guild]

        payload = api.settings_payload(guild)

        self.assertTrue(payload["timeout_settings_enabled"])
        self.assertTrue(payload["whitelist_user_names_enabled"])
        self.assertEqual(payload["modules"]["antichannel"]["timeout_minutes"], 90)
        self.assertEqual(payload["modules"]["antispam"]["timeout_minutes"], 10)
        self.assertEqual(payload["modules"]["antiraid"]["timeout_minutes"], 40320)
        self.assertEqual(payload["welcome"]["color"], "#2ECC71")
        self.assertEqual(payload["welcome"]["title"], "Welcome")
        self.assertEqual(payload["welcome"]["style"], "image_card")
        self.assertEqual(payload["welcome"]["font_size"], 48)
        self.assertEqual(payload["welcome"]["title_font_size"], 60)
        self.assertEqual(payload["welcome"]["video_url"], "https://youtu.be/example")
        self.assertTrue(payload["welcome"]["dm_enabled"])
        self.assertEqual(
            payload["emojis"],
            [
                {"name": "kenji_crown_3", "id": "456", "animated": False},
                {"name": "shared_emoji", "id": "789", "animated": True},
            ],
        )

    async def test_welcome_color_is_saved_and_invalid_color_is_rejected(self):
        state = {"settings": {"123": {"welcome": {"message": "Existing", "color": "#2ECC71"}}}}
        api = self.make_api(state)

        response = await api.update_settings(self.request({"welcome": {"color": "#a1B2c3"}}))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["welcome"]["color"], "#A1B2C3")

        response = await api.update_settings(self.request({"welcome": {"color": "red"}}))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["color"], "#A1B2C3")

    async def test_welcome_title_is_saved_and_limited_to_256_characters(self):
        state = {"settings": {"123": {"welcome": {"message": "Existing"}}}}
        api = self.make_api(state)

        response = await api.update_settings(self.request({"welcome": {"title": "Hello {name}"}}))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["welcome"]["title"], "Hello {name}")

        response = await api.update_settings(self.request({"welcome": {"title": "x" * 257}}))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["title"], "Hello {name}")

    async def test_welcome_media_and_card_options_are_saved_and_validated(self):
        state = {"settings": {"123": {"welcome": {"message": "Existing"}}}}
        api = self.make_api(state)

        response = await api.update_settings(self.request({
            "welcome": {
                "video_url": "https://youtu.be/example",
                "style": "image_card",
                "font_size": 48,
                "title_font_size": 60,
                "dm_enabled": True,
            },
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["welcome"]["video_url"], "https://youtu.be/example")
        self.assertEqual(state["settings"]["123"]["welcome"]["style"], "image_card")
        self.assertEqual(state["settings"]["123"]["welcome"]["font_size"], 48)
        self.assertEqual(state["settings"]["123"]["welcome"]["title_font_size"], 60)
        self.assertTrue(state["settings"]["123"]["welcome"]["dm_enabled"])

        response = await api.update_settings(self.request({
            "welcome": {"video_url": "javascript:alert(1)"},
        }))
        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["video_url"], "https://youtu.be/example")

        response = await api.update_settings(self.request({
            "welcome": {"style": "unknown", "font_size": 999},
        }))
        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["style"], "image_card")
        self.assertEqual(state["settings"]["123"]["welcome"]["font_size"], 48)
        self.assertEqual(state["settings"]["123"]["welcome"]["title_font_size"], 60)
        response = await api.update_settings(self.request({
            "welcome": {"title_font_size": 73},
        }))
        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["title_font_size"], 60)

    async def test_leave_channel_is_saved_and_rejects_channels_outside_the_server(self):
        state = {"settings": {"123": {"welcome": {"channel_id": 456}}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(id=123),
        ))
        selected_channel = SimpleNamespace(id=789)

        with patch.object(DashboardAPI, "text_channel", return_value=selected_channel):
            response = await api.update_settings(self.request({
                "welcome": {"leave_channel_id": "789"},
            }))

        self.assertEqual(response.status, 200)
        self.assertEqual(state["settings"]["123"]["welcome"]["leave_channel_id"], 789)

        with patch.object(DashboardAPI, "text_channel", return_value=None):
            response = await api.update_settings(self.request({
                "welcome": {"leave_channel_id": "999"},
            }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"]["leave_channel_id"], 789)

    async def test_settings_payload_leaves_leave_channel_unset_for_welcome_fallback(self):
        api = object.__new__(DashboardAPI)
        api.state = {"settings": {"123": {"welcome": {"channel_id": 456}}}}
        api.bot = SimpleNamespace(user=None, guilds=[])
        api.has_tier = lambda _guild_id, _tier: False
        guild = SimpleNamespace(
            id=123,
            name="Test server",
            icon=None,
            roles=[],
            text_channels=[],
            emojis=[],
            me=None,
        )

        payload = api.settings_payload(guild)

        self.assertEqual(payload["welcome"]["channel_id"], "456")
        self.assertEqual(payload["welcome"]["leave_channel_id"], "")

    async def test_welcome_shortcodes_render_inside_the_colored_embed(self):
        cog = object.__new__(WelcomerCog)
        cog.state = {"settings": {"123": {"welcome": {"color": "#123ABC", "title": "Hello {name}"}}}}
        other_guild = SimpleNamespace(
            id=456,
            emojis=[SimpleNamespace(name="shared_emoji", id=321, animated=True)],
        )
        cog.bot = SimpleNamespace(guilds=[other_guild])
        guild = SimpleNamespace(
            id=123,
            name="Test server",
            member_count=42,
            emojis=[
                SimpleNamespace(name="kenji_crown_3", id=456, animated=False),
                SimpleNamespace(name="kenji_aniblackheart", id=789, animated=True),
                SimpleNamespace(name="kenji_Black_line", id=1422605123456789012, animated=False),
            ],
        )
        member = SimpleNamespace(
            guild=guild,
            mention="<@12345678901234567>",
            display_name="Aarav",
            display_avatar=SimpleNamespace(url="https://cdn.example.com/avatar.png"),
        )

        payload = cog.welcome_payload(
            member,
            ":kenji_crown_3: **WELCOME {name}** :kenji_aniblackheart:",
        )

        self.assertNotIn("content", payload)
        self.assertEqual(
            payload["embed"].description,
            "<:kenji_crown_3:456> **WELCOME Aarav** <a:kenji_aniblackheart:789>",
        )
        self.assertEqual(payload["embed"].color.value, 0x123ABC)
        self.assertEqual(payload["embed"].title.split(" ", 1)[-1], "Hello Aarav")
        self.assertEqual(
            cog.render("<:kenjiBlackLine:1422605", member),
            "<:kenji_Black_line:1422605123456789012>",
        )
        self.assertEqual(
            cog.render("<a:external_emoji:1422605209141510305>", member),
            "<a:external_emoji:1422605209141510305>",
        )
        with patch.dict(
            ui.WELCOME_EMOJI_MAP,
            {"1422605209141510305": "<a:kx_welcome_1422605209141510305:987654321098765432>"},
        ):
            self.assertEqual(
                cog.render("<a:external_emoji:1422605209141510305>", member),
                "<a:kx_welcome_1422605209141510305:987654321098765432>",
            )
        self.assertEqual(
            cog.render(
                "<:kenji_Black_line:1422605503183323279> "
                "<:kenji_Black_line:1422605",
                member,
            ),
            "<:kenji_Black_line:1422605503183323279> "
            "<:kenji_Black_line:1422605503183323279>",
        )
        self.assertEqual(
            cog.render("<:external_emoji:1422605", member),
            "<:external_emoji:1422605",
        )
        self.assertEqual(
            cog.render(":sharedemoji:", member),
            "<a:shared_emoji:321>",
        )

    async def test_welcome_message_accepts_up_to_2000_characters(self):
        message = "x" * 1500
        api = self.make_api({"settings": {"123": {"welcome": {}}}})

        response = await api.update_settings(self.request({"welcome": {"message": message}}))

        self.assertEqual(response.status, 200)
        self.assertEqual(api.state["settings"]["123"]["welcome"]["message"], message)

        response = await api.update_settings(self.request({"welcome": {"message": "x" * 2001}}))

        self.assertEqual(response.status, 400)

    async def test_welcome_emoji_images_are_reused_as_application_emojis(self):
        emoji_id = "1422605209141510305"
        app_markup = "<a:kx_welcome_1422605209141510305:987654321098765432>"

        class ApplicationEmoji:
            name = f"kx_welcome_{emoji_id}"

            def __str__(self):
                return app_markup

        class SecondaryEmoji:
            name = "spider_secondary"

            def __str__(self):
                return "<:spider_secondary:123456789012345678>"

        bot = SimpleNamespace(
            fetch_application_emojis=AsyncMock(return_value=[ApplicationEmoji(), SecondaryEmoji()]),
            create_application_emoji=AsyncMock(),
        )
        state = {
            "settings": {
                "123": {
                    "welcome": {
                        "message": f"<a:kenji_crown_3:{emoji_id}>",
                    },
                },
            },
        }

        with patch.dict(ui.WELCOME_EMOJI_MAP, {}, clear=True):
            await ui.setup_emojis(bot, state)

            self.assertEqual(ui.WELCOME_EMOJI_MAP[emoji_id], app_markup)
        self.assertEqual(
            {call.kwargs["name"] for call in bot.create_application_emoji.await_args_list},
            set(ui.EMBED_ICON_ASSETS),
        )

    async def test_animated_embed_icons_are_uploaded_and_cached(self):
        class ApplicationEmoji:
            def __init__(self, name):
                self.name = name
                self.url = f"https://cdn.example/{name}.gif"

            def __str__(self):
                return f"<a:{self.name}:123456789012345678>"

        bot = SimpleNamespace(
            fetch_application_emojis=AsyncMock(return_value=[]),
            create_application_emoji=AsyncMock(
                side_effect=lambda name, image: ApplicationEmoji(name)
            ),
        )

        with (
            patch.dict(ui.EMBED_ICON_URLS, {}, clear=True),
            patch.dict(ui.EMBED_EMOJI_MENTIONS, {}, clear=True),
            patch.object(ui, "SECONDARY_SPIDER", ui.FALLBACK_SECONDARY),
        ):
            await ui.setup_emojis(bot, {})

            self.assertEqual(
                ui.EMBED_ICON_URLS,
                {
                    name: f"https://cdn.example/{name}.gif"
                    for name in ui.EMBED_ICON_ASSETS
                },
            )
            self.assertEqual(
                ui.EMBED_EMOJI_MENTIONS,
                {
                    name: f"<a:{name}:123456789012345678>"
                    for name in ui.EMBED_ICON_ASSETS
                },
            )

        uploaded = {call.kwargs["name"]: call.kwargs["image"] for call in bot.create_application_emoji.await_args_list}
        for name, filename in ui.EMBED_ICON_ASSETS.items():
            self.assertEqual(uploaded[name], (ui.EMBED_ICON_DIR / filename).read_bytes())

    async def test_missing_welcome_emoji_is_copied_from_discord_cdn(self):
        emoji_id = "1422605209141510305"
        app_markup = f"<a:kx_welcome_{emoji_id}:987654321098765432>"
        requested_urls = []

        class ApplicationEmoji:
            name = f"kx_welcome_{emoji_id}"

            def __str__(self):
                return app_markup

        class SecondaryEmoji:
            name = "spider_secondary"

            def __str__(self):
                return "<:spider_secondary:123456789012345678>"

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def raise_for_status(self):
                return None

            async def read(self):
                return b"animated-gif"

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, url):
                requested_urls.append(url)
                return Response()

        bot = SimpleNamespace(
            fetch_application_emojis=AsyncMock(return_value=[SecondaryEmoji()]),
            create_application_emoji=AsyncMock(return_value=ApplicationEmoji()),
        )
        state = {
            "settings": {
                "123": {
                    "welcome": {
                        "message": f"<a:kenji_crown_3:{emoji_id}>",
                    },
                },
            },
        }

        with (
            patch.dict(ui.WELCOME_EMOJI_MAP, {}, clear=True),
            patch("botcore.ui.aiohttp.ClientSession", side_effect=Session),
        ):
            await ui.setup_emojis(bot, state)

            self.assertEqual(
                requested_urls,
                [f"https://cdn.discordapp.com/emojis/{emoji_id}.gif?size=128&quality=lossless"],
            )
            self.assertEqual(ui.WELCOME_EMOJI_MAP[emoji_id], app_markup)
        self.assertIn(
            {
                "name": f"kx_welcome_{emoji_id}",
                "image": b"animated-gif",
            },
            [call.kwargs for call in bot.create_application_emoji.await_args_list],
        )

    async def test_whitelist_user_names_use_cached_discord_usernames(self):
        api = object.__new__(DashboardAPI)
        member = SimpleNamespace(name="actual_username")
        api.bot = SimpleNamespace(fetch_user=AsyncMock())
        guild = SimpleNamespace(get_member=lambda _user_id: member)

        names = await api.whitelist_user_names(guild, ["12345678901234567"])

        self.assertEqual(names, {"12345678901234567": "actual_username"})
        api.bot.fetch_user.assert_not_awaited()

    async def test_security_timeout_action_uses_configured_minutes(self):
        cog = object.__new__(SecurityCog)
        cog.state = {"settings": {"123": {"security": {
            "punishments": {"antichannel": "timeout"},
            "timeouts": {"antichannel": 90},
        }}}}
        cog.can_punish = lambda *_args: None
        cog.dm_punished = AsyncMock()
        member = SimpleNamespace(timeout=AsyncMock())
        guild = SimpleNamespace(id=123, get_member=lambda _user_id: member)

        result = await cog.punish(guild, 456, "antichannel", "test reason")

        self.assertEqual(result, (True, "timeout", None))
        member.timeout.assert_awaited_once_with(timedelta(minutes=90), reason="test reason")
        cog.dm_punished.assert_awaited_once_with(
            guild,
            456,
            "timeout",
            "test reason",
            "antichannel",
            "90 minutes",
        )

    async def test_welcome_image_url_is_saved(self):
        state = {"settings": {"123": {"welcome": {"message": "Existing"}}}}
        api = self.make_api(state)
        saved = []
        api.save = lambda: saved.append(True)

        response = await api.update_settings(self.request({
            "welcome": {"image_url": "https://cdn.example.com/welcome.png?size=large"},
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            state["settings"]["123"]["welcome"]["image_url"],
            "https://cdn.example.com/welcome.png?size=large",
        )
        self.assertEqual(saved, [True])

    async def test_invalid_welcome_image_url_is_rejected_atomically(self):
        state = {"settings": {"123": {"welcome": {"message": "Existing"}}}}
        api = self.make_api(state)

        response = await api.update_settings(self.request({
            "welcome": {
                "message": "Should not be saved",
                "image_url": "javascript:alert(1)",
            },
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["welcome"], {"message": "Existing"})

    async def test_oauth_state_is_bound_to_the_browser_cookie(self):
        api = self.make_api({})
        api.config = DashboardConfig("client-id", "client-secret", "https://kryo-web.vercel.app", 8080)
        api._oauth_states = {}

        login_response = await api.login(SimpleNamespace())
        state = parse_qs(urlsplit(login_response.headers["Location"]).query)["state"][0]
        self.assertIn(OAUTH_STATE_COOKIE, login_response.cookies)

        callback_response = await api.callback(SimpleNamespace(
            query={"state": state, "code": "authorization-code"},
            cookies={OAUTH_STATE_COOKIE: "a-different-browser-state"},
        ))

        self.assertEqual(callback_response.status, 400)

    async def test_session_is_restored_after_api_restart(self):
        state = {}
        config = DashboardConfig("client-id", "client-secret", "https://kryo-web.vercel.app", 8080)
        api = DashboardAPI(cast(Bot, SimpleNamespace()), state, lambda: None, lambda _guild_id, _tier: False, config)
        api._sessions["session-id"] = DashboardSession(
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=4_000_000_000,
            user={"id": "123", "username": "KRYO user"},
            expires_at=4_000_000_000,
        )
        api.persist_sessions()

        restarted_api = DashboardAPI(cast(Bot, SimpleNamespace()), state, lambda: None, lambda _guild_id, _tier: False, config)
        session = restarted_api.session(cast(Request, SimpleNamespace(cookies={"kryo_dashboard": "session-id"})))

        self.assertIsNotNone(session)
        if session is None:
            self.fail("The persisted dashboard session was not restored.")
        self.assertEqual(session.user["username"], "KRYO user")
        self.assertEqual(SESSION_SECONDS, 30 * 24 * 60 * 60)

    async def test_expired_access_token_is_refreshed_and_saved(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return {
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                }

        class HTTP:
            def post(self, *_args, **_kwargs):
                return Response()

        state = {}
        saved = []
        api = self.make_api(state)
        api.config = DashboardConfig("client-id", "client-secret", "https://kryo-web.vercel.app", 8080)
        api._http = cast(ClientSession, HTTP())
        api._sessions = {}
        api.save = lambda: saved.append(True)
        session = DashboardSession(
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            access_token_expires_at=0,
            user={"id": "123", "username": "KRYO user"},
            expires_at=4_000_000_000,
        )
        api._sessions["session-id"] = session

        result = await api.refresh_access_token(session)

        self.assertIsNone(result)
        self.assertEqual(session.access_token, "new-access-token")
        self.assertEqual(session.refresh_token, "new-refresh-token")
        self.assertEqual(state["dashboard_sessions"]["session-id"]["access_token"], "new-access-token")
        self.assertEqual(saved, [True])

    async def test_discord_guild_access_is_cached_for_repeated_dashboard_saves(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return [{"id": "123", "permissions": str(1 << 5)}]

        class HTTP:
            def __init__(self):
                self.get_calls = 0

            def get(self, *_args, **_kwargs):
                self.get_calls += 1
                return Response()

        api = object.__new__(DashboardAPI)
        http = HTTP()
        api._http = cast(ClientSession, http)
        session = DashboardSession(
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=4_000_000_000,
            user={"id": "456", "username": "KRYO user"},
            expires_at=4_000_000_000,
        )

        with patch("botcore.dashboard_api.time.time", return_value=1000):
            first = await api.discord_guilds(session)
            second = await api.discord_guilds(session)

        self.assertEqual(first, [{"id": "123", "permissions": str(1 << 5)}])
        self.assertEqual(second, first)
        self.assertEqual(http.get_calls, 1)
        self.assertEqual(
            session.guild_access_expires_at,
            1000 + GUILD_ACCESS_CACHE_SECONDS,
        )

    async def test_invite_tracker_settings_validate_permissions_and_save(self):
        state = {"settings": {"123": {"invite_tracker": {"enabled": False}}}}
        api = self.make_api(state)
        bot_member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))
        api.authorized_guild = AsyncMock(return_value=(object(), SimpleNamespace(id=123, me=bot_member)))
        saved = []
        api.save = lambda: saved.append(True)

        response = await api.update_settings(self.request({
            "invite_tracker": {
                "enabled": True,
                "log_channel_id": "",
                "minimum_account_age_days": 14,
            },
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            state["settings"]["123"]["invite_tracker"],
            {
                "enabled": True,
                "log_channel_id": 0,
                "minimum_account_age_days": 14,
            },
        )
        self.assertEqual(saved, [True])

        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(id=123, me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_guild=False),
            )),
        ))
        response = await api.update_settings(self.request({
            "invite_tracker": {"enabled": False},
        }))
        self.assertEqual(response.status, 200)

        state["settings"]["123"]["invite_tracker"]["enabled"] = False
        before = dict(state["settings"]["123"]["invite_tracker"])
        response = await api.update_settings(self.request({
            "invite_tracker": {"enabled": True},
        }))
        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["invite_tracker"], before)

    async def test_dashboard_announcement_sends_embed_with_explicit_mention_policy(self):
        channel = SimpleNamespace(
            id=456,
            permissions_for=lambda _member: SimpleNamespace(
                send_messages=True,
                embed_links=True,
                mention_everyone=True,
            ),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=123,
            name="Demo",
            icon=None,
            me=object(),
            get_channel=lambda _channel_id: channel,
        )
        api = self.make_api({})
        api.authorized_guild = AsyncMock(return_value=(object(), guild))

        with patch.object(DashboardAPI, "text_channel", return_value=channel):
            response = await api.send_announcement(self.request({
                "channel_id": "456",
                "title": "Server news",
                "message": "@everyone Update is live. @here",
                "color": "#AABBCC",
                "allow_everyone": True,
            }))

        self.assertEqual(response.status, 200)
        channel.send.assert_awaited_once()
        sent = channel.send.await_args.kwargs
        self.assertEqual(sent["embed"].title, "Server news")
        self.assertEqual(sent["content"], "@everyone @here")
        self.assertEqual(sent["embed"].description, "Update is live.")
        self.assertEqual(sent["embed"].footer.text, "OFFICIAL SERVER ANNOUNCEMENT  •  KRYO")
        self.assertEqual(sent["embed"].author.name, "KRYO  •  Demo")
        self.assertIsNotNone(sent["embed"].timestamp)
        self.assertTrue(sent["allowed_mentions"].everyone)
        self.assertFalse(sent["allowed_mentions"].roles)
        self.assertFalse(sent["allowed_mentions"].users)

    async def test_dashboard_announcement_rejects_unpermitted_pings_before_sending(self):
        channel = SimpleNamespace(
            id=456,
            permissions_for=lambda _member: SimpleNamespace(
                send_messages=True,
                embed_links=True,
                mention_everyone=False,
            ),
            send=AsyncMock(),
        )
        api = self.make_api({})
        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(id=123, name="Demo", icon=None, me=object()),
        ))

        with patch.object(DashboardAPI, "text_channel", return_value=channel):
            response = await api.send_announcement(self.request({
                "channel_id": "456",
                "message": "@everyone Alert",
                "color": "#AABBCC",
                "allow_everyone": True,
            }))

        self.assertEqual(response.status, 403)
        channel.send.assert_not_awaited()

    async def test_ticket_settings_save_categories_questions_and_private_channels(self):
        state = {"settings": {"123": {"existing": "preserved"}}}
        api = self.make_api(state)
        bot_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_channels=True),
            top_role=object(),
        )
        guild = SimpleNamespace(id=123, me=bot_member)
        api.authorized_guild = AsyncMock(return_value=(object(), guild))
        api.settings_payload = lambda _guild, _user_id=None: {"whitelist": {"users": []}}
        channel = SimpleNamespace(
            id=456,
            permissions_for=lambda _member: SimpleNamespace(
                send_messages=True,
                embed_links=True,
                attach_files=True,
            ),
        )
        saved = []
        api.save = lambda: saved.append(True)
        payload = {
            "ticketing": {
                "enabled": True,
                "panel_channel_id": "456",
                "ticket_category_id": "",
                "staff_role_ids": [],
                "transcript_channel_id": "789",
                "panel_title": "Contact support",
                "panel_description": "Pick a category to get help.",
                "panel_tagline": "HELP · SUPPORT",
                "panel_tagline_description": "Describe what you need.",
                "panel_next_title": "Next",
                "panel_next_description": "Our team will reply soon.",
                "panel_footer": "Thanks for contacting us.",
                "panel_color": "#aabbcc",
                "panel_image_url": "https://cdn.example/ticket-panel.png",
                "open_limit": 2,
                "categories": [{
                    "key": "billing",
                    "label": "Billing",
                    "description": "Payments and plans",
                    "emoji": "💳",
                    "questions": ["What is your order ID?"],
                }],
            },
        }

        with patch.object(DashboardAPI, "text_channel", return_value=channel):
            response = await api.update_settings(self.request(payload))

        self.assertEqual(response.status, 200)
        self.assertEqual(saved, [True])
        ticketing = state["settings"]["123"]["tickets"]
        self.assertTrue(ticketing["enabled"])
        self.assertEqual(ticketing["panel_channel_id"], 456)
        self.assertEqual(ticketing["panel_color"], "#AABBCC")
        self.assertEqual(ticketing["panel_image_url"], "https://cdn.example/ticket-panel.png")
        self.assertEqual(ticketing["panel_tagline"], "HELP · SUPPORT")
        self.assertEqual(ticketing["panel_tagline_description"], "Describe what you need.")
        self.assertEqual(ticketing["panel_next_title"], "Next")
        self.assertEqual(ticketing["panel_next_description"], "Our team will reply soon.")
        self.assertEqual(ticketing["panel_footer"], "Thanks for contacting us.")
        self.assertEqual(ticketing["categories"][0]["questions"], ["What is your order ID?"])
        self.assertEqual(state["settings"]["123"]["existing"], "preserved")

    async def test_ticket_panel_text_fields_reject_oversized_input(self):
        state = {"settings": {"123": {"tickets": {"panel_footer": "Current footer"}}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(id=123, me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True))),
        ))

        response = await api.update_settings(self.request({
            "ticketing": {"panel_tagline": "x" * 221},
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["tickets"]["panel_footer"], "Current footer")
        self.assertNotIn("panel_tagline", state["settings"]["123"]["tickets"])

    async def test_invalid_ticket_panel_image_url_is_rejected_without_saving(self):
        state = {"settings": {"123": {"tickets": {"panel_title": "Existing"}}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(id=123, me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True))),
        ))

        response = await api.update_settings(self.request({
            "ticketing": {"panel_image_url": "javascript:alert(1)"},
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["tickets"]["panel_title"], "Existing")
        self.assertNotIn("panel_image_url", state["settings"]["123"]["tickets"])

    async def test_invalid_ticket_question_does_not_partially_update_settings(self):
        state = {"settings": {"123": {"tickets": {"panel_title": "Existing"}}}}
        api = self.make_api(state)
        api.authorized_guild = AsyncMock(return_value=(
            object(),
            SimpleNamespace(
                id=123,
                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)),
            ),
        ))
        api.settings_payload = lambda _guild, _user_id=None: {"whitelist": {"users": []}}

        response = await api.update_settings(self.request({
            "ticketing": {
                "panel_title": "Would be partial",
                "categories": [{
                    "key": "support",
                    "label": "Support",
                    "questions": ["x" * 46],
                }],
            },
        }))

        self.assertEqual(response.status, 400)
        self.assertEqual(state["settings"]["123"]["tickets"]["panel_title"], "Existing")


if __name__ == "__main__":
    unittest.main()
