import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from music_panel import MusicPanel


class SlashPlaybackResponseTests(unittest.IsolatedAsyncioTestCase):
    def make_interaction(self):
        return SimpleNamespace(
            guild=object(),
            guild_id=1,
            user=SimpleNamespace(id=2),
            channel=object(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            delete_original_response=AsyncMock(),
        )

    async def test_successful_playback_deletes_deferred_status_message(self) -> None:
        panel = MusicPanel.__new__(MusicPanel)
        panel.autocomplete_tracks = {}
        panel.play_query = AsyncMock(return_value=None)
        interaction = self.make_interaction()

        await MusicPanel.slash_play.callback(panel, interaction, "song query")

        interaction.delete_original_response.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()

    async def test_playback_errors_and_queue_updates_are_still_reported(self) -> None:
        for result in ("No playable results were found.", "Added **Song** to the queue."):
            with self.subTest(result=result):
                panel = MusicPanel.__new__(MusicPanel)
                panel.autocomplete_tracks = {}
                panel.play_query = AsyncMock(return_value=result)
                interaction = self.make_interaction()

                await MusicPanel.slash_play.callback(panel, interaction, "song query")

                interaction.followup.send.assert_awaited_once_with(result)
                interaction.delete_original_response.assert_not_awaited()
