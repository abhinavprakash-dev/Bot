import os
import unittest
from unittest.mock import patch


class MusicSearchSourceTests(unittest.TestCase):
    def test_default_is_ytmsearch_when_unset(self):
        os.environ.pop("MUSIC_SEARCH_SOURCE", None)
        import importlib
        import music_panel
        importlib.reload(music_panel)
        self.assertEqual(music_panel.SEARCH_SOURCE, "ytmsearch")

    def test_explicit_override_is_respected(self):
        with patch.dict(os.environ, {"MUSIC_SEARCH_SOURCE": "scsearch"}):
            import importlib
            import music_panel
            importlib.reload(music_panel)
            self.assertEqual(music_panel.SEARCH_SOURCE, "scsearch")


class MusicSearchProviderPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_result_wins_before_better_scored_soundcloud_result(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import music_panel

        youtube_track = SimpleNamespace(
            identifier="youtube-1",
            title="Nazar Lagi by Asees Kaur",
            author="Asees Kaur",
            length=200000,
            is_stream=False,
            source="youtube",
        )
        soundcloud_track = SimpleNamespace(
            identifier="soundcloud-1",
            title="Nazar Lagi",
            author="Asees Kaur",
            length=200000,
            is_stream=False,
            source="soundcloud",
        )

        responses = {
            "ytmsearch": [],
            "ytsearch": [youtube_track],
            "scsearch": [soundcloud_track],
            "bcsearch": [],
        }
        calls = []

        async def fake_search(query, *, source):
            calls.append(source)
            return responses[source]

        state = SimpleNamespace(
            guild_id=123,
            blocked_sources={},
            source_failures={},
        )
        panel = object.__new__(music_panel.MusicPanel)

        with patch.object(
            music_panel.wavelink.Playable,
            "search",
            new=AsyncMock(side_effect=fake_search),
        ):
            tracks, playlist_name, error = await panel.search_tracks(
                "Nazar Lagi",
                preferred="ytmsearch",
                state=state,
            )

        self.assertIsNone(error)
        self.assertIsNone(playlist_name)
        self.assertEqual(tracks, [youtube_track])
        self.assertEqual(calls, ["ytmsearch", "ytsearch"])


if __name__ == "__main__":
    unittest.main()
