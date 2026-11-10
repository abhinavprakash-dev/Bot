import unittest

from botcore.link_filter import LinkFilterConfig, contains_link


class LinkDetectorTests(unittest.TestCase):
    def setUp(self):
        self.config = LinkFilterConfig()

    def test_detects_protocol_url(self):
        self.assertTrue(contains_link("Visit https://example.com/path", self.config)[0])

    def test_detects_www_host(self):
        self.assertTrue(contains_link("www.example.com", self.config)[0])

    def test_detects_discord_invite(self):
        self.assertTrue(contains_link("discord.gg/example", self.config)[0])

    def test_detects_bare_domain(self):
        self.assertTrue(contains_link("example.com/path", self.config)[0])

    def test_ignores_whitelisted_domain(self):
        config = LinkFilterConfig(whitelisted_domains=frozenset({"youtube.com"}))
        self.assertFalse(contains_link("https://youtube.com/watch?v=1", config)[0])

    def test_ignores_non_link_text(self):
        self.assertFalse(contains_link("e.g. file.txt hello.world", self.config)[0])

    def test_detects_markdown_url(self):
        self.assertTrue(contains_link("[text](https://example.com)", self.config)[0])
