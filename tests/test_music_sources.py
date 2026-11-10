"""Unit tests for botcore.music_sources (standard library only)."""
import unittest
from types import SimpleNamespace
from botcore.music_sources import (
    is_spotify_link, normalize_audio_source, ordered_search_sources,
    record_source_failure, record_source_success, spotify_open_url,
)


class SpotifyURLTests(unittest.TestCase):
    def test_track_playlist_album_and_uri(self):
        for value in (
            "https://open.spotify.com/track/123ABC?si=abc",
            "https://open.spotify.com/album/123ABC",
            "https://open.spotify.com/playlist/123ABC",
            "https://open.spotify.com/intl-in/track/123ABC",
            "spotify:track:123ABC",
            "https://spotify.link/shortCode",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_spotify_link(value))

    def test_spotify_uri_becomes_clickable_https_url(self):
        self.assertEqual(spotify_open_url("spotify:track:123ABC"), "https://open.spotify.com/track/123ABC")

    def test_rejects_unrelated_and_spoofed_hosts(self):
        for value in ("https://youtube.com/watch?v=abc", "https://spotify.com.attacker.example/track/123", "not a URL"):
            with self.subTest(value=value):
                self.assertFalse(is_spotify_link(value))


class SourceOrderTests(unittest.TestCase):
    def test_spotify_mirror_prefix_is_not_an_audio_source(self):
        self.assertEqual(normalize_audio_source("spsearch", "ytmsearch"), "ytmsearch")
        self.assertEqual(normalize_audio_source("unknown", "scsearch"), "scsearch")

    def test_preferred_then_fallback_then_remaining_sources(self):
        state = SimpleNamespace(blocked_sources={}, source_failures={})
        self.assertEqual(ordered_search_sources("scsearch", "ytmsearch", state), ["scsearch", "ytmsearch", "ytsearch", "bcsearch"])

    def test_expired_cooldown_is_removed(self):
        state = SimpleNamespace(blocked_sources={"ytmsearch": 90.0}, source_failures={"ytmsearch": 3})
        sources = ordered_search_sources("ytmsearch", "scsearch", state, now=100.0)
        self.assertIn("ytmsearch", sources)
        self.assertNotIn("ytmsearch", state.blocked_sources)

    def test_active_cooldown_is_respected(self):
        state = SimpleNamespace(blocked_sources={"ytmsearch": 200.0}, source_failures={"ytmsearch": 3})
        sources = ordered_search_sources("ytmsearch", "scsearch", state, now=100.0)
        self.assertNotIn("ytmsearch", sources)
        self.assertEqual(sources[0], "scsearch")

    def test_three_failures_apply_five_minute_cooldown(self):
        state = SimpleNamespace(blocked_sources={}, source_failures={})
        self.assertEqual(record_source_failure(state, "scsearch", now=100.0), 1)
        self.assertEqual(record_source_failure(state, "scsearch", now=100.0), 2)
        self.assertEqual(record_source_failure(state, "scsearch", now=100.0), 3)
        self.assertEqual(state.blocked_sources["scsearch"], 400.0)

    def test_success_resets_failure_streak_and_cooldown(self):
        state = SimpleNamespace(blocked_sources={"scsearch": 400.0}, source_failures={"scsearch": 3})
        record_source_success(state, "scsearch")
        self.assertNotIn("scsearch", state.blocked_sources)
        self.assertNotIn("scsearch", state.source_failures)


if __name__ == "__main__":
    unittest.main()
