import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import discord

from cogs.ownerhub import OwnerHub, _load, _save


class OwnerHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmpdir, "owner_hub_guilds.json")
        os.environ["OWNER_HUB_STORE_DIR"] = self.tmpdir
        # reload module-level path
        import cogs.ownerhub as oh
        oh.STORE_PATH = __import__("pathlib").Path(self.store_path)
        oh._STORE_DIR = __import__("pathlib").Path(self.tmpdir)

    def tearDown(self):
        os.environ.pop("OWNER_HUB_STORE_DIR", None)

    def test_reconcile_preserves_history(self):
        data = {"123": {"name": "Old", "first_seen": "2025-01-01", "status": "Joined"}}
        _save(data)
        bot = MagicMock()
        guild = MagicMock()
        guild.id = 123
        guild.name = "New Name"
        guild.owner_id = 999
        guild.member_count = 10
        bot.guilds = [guild]
        cog = OwnerHub.__new__(OwnerHub)
        cog.bot = bot
        cog.hub_guild_id = 0
        cog.log_channel_id = 0
        cog.dashboard_channel_id = 0
        cog.protected_owners = ()
        cog.guilds = _load()
        cog._reconcile()
        self.assertEqual(cog.guilds["123"]["first_seen"], "2025-01-01")
        self.assertEqual(cog.guilds["123"]["name"], "New Name")

    def test_new_guild_labelled_initial_sync(self):
        bot = MagicMock()
        guild = MagicMock()
        guild.id = 456
        guild.name = "NewGuild"
        guild.owner_id = 1
        guild.member_count = 5
        bot.guilds = [guild]
        cog = OwnerHub.__new__(OwnerHub)
        cog.bot = bot
        cog.hub_guild_id = 0
        cog.log_channel_id = 0
        cog.dashboard_channel_id = 0
        cog.protected_owners = ()
        cog.guilds = {}
        cog._reconcile()
        self.assertEqual(cog.guilds["456"]["status"], "Already Present - Initial Sync")

    def test_unauthorized_user_denied(self):
        # just ensure _is_owner works
        cog = OwnerHub.__new__(OwnerHub)
        cog.protected_owners = (999,)
        self.assertFalse(cog._is_owner(123))
        self.assertTrue(cog._is_owner(999))


if __name__ == "__main__":
    unittest.main()
