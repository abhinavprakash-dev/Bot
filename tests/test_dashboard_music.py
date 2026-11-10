import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botcore.dashboard_api import DashboardAPI
from music_panel import MusicPanel


class DashboardMusicTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(payload):
        return SimpleNamespace(json=AsyncMock(return_value=payload))

    def make_api(self, cog, guild):
        api = object.__new__(DashboardAPI)
        api.bot = SimpleNamespace(get_cog=lambda name: cog if name == "MusicPanel" else None)
        api.authorized_guild = AsyncMock(return_value=(
            SimpleNamespace(user={"id": "456"}),
            guild,
        ))
        return api

    async def test_dashboard_saves_247_voice_preferences(self):
        class FakeVoiceChannel:
            id = 789
            name = "Music"

            @staticmethod
            def permissions_for(_member):
                return SimpleNamespace(view_channel=True, connect=True, speak=True)

        state = SimpleNamespace(stay_247=False, stay_channel_id=None)
        cog = SimpleNamespace(
            state=Mock(return_value=state),
            persist_247_settings=Mock(),
        )
        member = SimpleNamespace()
        channel = FakeVoiceChannel()
        guild = SimpleNamespace(
            id=123,
            me=SimpleNamespace(),
            get_member=lambda member_id: member if member_id == 456 else None,
            get_channel=lambda channel_id: channel if channel_id == channel.id else None,
        )
        api = self.make_api(cog, guild)

        with patch("botcore.dashboard_api.discord.VoiceChannel", FakeVoiceChannel):
            response = await api.music_action(self.request({
                "action": "stay247",
                "enabled": True,
                "channel_id": str(channel.id),
            }))

        self.assertEqual(response.status, 200)
        self.assertTrue(state.stay_247)
        self.assertEqual(state.stay_channel_id, channel.id)
        cog.persist_247_settings.assert_called_once_with()
        self.assertTrue(json.loads(response.text)["ok"])

    async def test_music_panel_actions_receive_player_and_state(self):
        class FakePlayer:
            paused = False

        state = SimpleNamespace()
        cog = SimpleNamespace(
            play_query=AsyncMock(),
            state=Mock(return_value=state),
            do_pause=AsyncMock(return_value=None),
        )
        member = SimpleNamespace()
        guild = SimpleNamespace(
            id=123,
            voice_client=FakePlayer(),
            get_member=lambda member_id: member if member_id == 456 else None,
        )
        api = self.make_api(cog, guild)

        with patch("botcore.dashboard_api.wavelink.Player", FakePlayer):
            response = await api.music_action(self.request({"action": "pause"}))

        self.assertEqual(response.status, 200)
        cog.do_pause.assert_awaited_once_with(guild.voice_client, state)

    async def test_queue_add_reports_success_for_music_panel_result(self):
        class FakePlayer:
            channel = SimpleNamespace()

        cog = SimpleNamespace(
            play_query=AsyncMock(return_value="Added **Song** to the queue."),
            state=Mock(return_value=SimpleNamespace()),
        )
        member = SimpleNamespace()
        guild = SimpleNamespace(
            id=123,
            voice_client=FakePlayer(),
            get_member=lambda member_id: member if member_id == 456 else None,
        )
        api = self.make_api(cog, guild)

        with patch("botcore.dashboard_api.wavelink.Player", FakePlayer):
            response = await api.music_action(self.request({
                "action": "queue_add",
                "query": "Song",
            }))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.text)["description"],
            "Added **Song** to the queue.",
        )
        cog.play_query.assert_awaited_once_with(guild, member, guild.voice_client.channel, "Song")

    async def test_dashboard_saves_music_card_and_access_settings(self):
        class FakeTextChannel:
            id = 987

            @staticmethod
            def permissions_for(_member):
                return SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                )

        state = SimpleNamespace(
            post_now_playing=True,
            text_channel_id=None,
            access_level="everyone",
            dj_role_id=None,
        )
        cog = SimpleNamespace(
            state=Mock(return_value=state),
            persist_247_settings=Mock(),
            remove_panel=AsyncMock(),
            send_panel=AsyncMock(),
        )
        role = SimpleNamespace(id=654)
        member = SimpleNamespace()
        guild = SimpleNamespace(
            id=123,
            me=SimpleNamespace(),
            voice_client=None,
            get_member=lambda member_id: member if member_id == 456 else None,
            get_channel=lambda channel_id: FakeTextChannel() if channel_id == 987 else None,
            get_role=lambda role_id: role if role_id == role.id else None,
        )
        api = self.make_api(cog, guild)

        with patch("botcore.dashboard_api.discord.TextChannel", FakeTextChannel):
            response = await api.music_action(self.request({
                "action": "music_settings",
                "post_now_playing": False,
                "panel_channel_id": "987",
                "access_level": "dj",
                "dj_role_id": "654",
            }))

        self.assertEqual(response.status, 200)
        self.assertFalse(state.post_now_playing)
        self.assertEqual(state.text_channel_id, 987)
        self.assertEqual(state.access_level, "dj")
        self.assertEqual(state.dj_role_id, 654)
        cog.persist_247_settings.assert_called_once_with()
        cog.remove_panel.assert_awaited_once_with(state)

    def test_music_access_level_enforces_dj_role_but_allows_managers(self):
        panel = object.__new__(MusicPanel)
        panel.states = {}
        guild = SimpleNamespace(id=123)
        state = panel.state(guild.id)
        state.access_level = "dj"
        state.dj_role_id = 654
        member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
            roles=[SimpleNamespace(id=789)],
        )

        self.assertEqual(
            panel.music_access_error(guild, member),
            "Music is limited to the configured DJ role.",
        )
        member.roles.append(SimpleNamespace(id=654))
        self.assertIsNone(panel.music_access_error(guild, member))
        member.roles.clear()
        member.guild_permissions.manage_guild = True
        self.assertIsNone(panel.music_access_error(guild, member))

    def test_music_247_and_autoplay_preferences_survive_panel_reload(self):
        stored = {
            "music_247": {"123": {"enabled": True, "channel_id": 789}},
            "music_preferences": {"123": {"autoplay": True}},
        }
        bot = SimpleNamespace(state=stored, save=Mock())
        panel = object.__new__(MusicPanel)
        panel.bot = bot
        panel.states = {}

        panel.restore_247_settings()
        restored = panel.state(123)

        self.assertTrue(restored.stay_247)
        self.assertEqual(restored.stay_channel_id, 789)
        self.assertTrue(restored.autoplay)

        panel.persist_247_settings()
        bot.save.assert_called_once_with()

        reloaded = object.__new__(MusicPanel)
        reloaded.bot = bot
        reloaded.states = {}
        reloaded.restore_247_settings()
        reloaded_state = reloaded.state(123)
        self.assertTrue(reloaded_state.stay_247)
        self.assertEqual(reloaded_state.stay_channel_id, 789)
        self.assertTrue(reloaded_state.autoplay)


if __name__ == "__main__":
    unittest.main()
