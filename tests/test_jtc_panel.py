import unittest
from typing import cast
from unittest.mock import patch

import discord

from botcore import vc_icons
from cogs.community import JtcPanel, MODAL_ACTIONS, PANEL_BUTTON_ROWS, USER_ACTIONS


EXPECTED_ROWS = (
    (
        ("Lock", "lock", discord.ButtonStyle.secondary),
        ("Unlock", "unlock", discord.ButtonStyle.secondary),
        ("Hide", "hide", discord.ButtonStyle.secondary),
        ("Unhide", "unhide", discord.ButtonStyle.secondary),
    ),
    (
        ("Permit", "permit", discord.ButtonStyle.success),
        ("Permit All", "permitall", discord.ButtonStyle.success),
        ("Invite", "invite", discord.ButtonStyle.primary),
    ),
    (
        ("Vc Ban", "vcban", discord.ButtonStyle.danger),
        ("VcUnban", "vcunban", discord.ButtonStyle.secondary),
        ("Mute", "mute", discord.ButtonStyle.secondary),
        ("Unmute", "unmute", discord.ButtonStyle.secondary),
    ),
    (
        ("Transfer", "transfer", discord.ButtonStyle.primary),
        ("Claim", "claim", discord.ButtonStyle.primary),
    ),
    (
        ("Rename", "rename", discord.ButtonStyle.secondary),
        ("Limit", "limit", discord.ButtonStyle.secondary),
        ("Bitrate", "bitrate", discord.ButtonStyle.secondary),
    ),
)


class PanelActionRecorder:
    def __init__(self):
        self.keys = []

    async def panel_action(self, interaction, key):
        self.keys.append(key)


class JoinToCreatePanelTests(unittest.TestCase):
    def test_layout_and_controls_are_persistent(self):
        panel = JtcPanel(None)
        controls = panel.children[1]
        rows = [
            item
            for item in controls.children
            if isinstance(item, discord.ui.ActionRow)
        ]
        actual = tuple(
            tuple(
                (
                    button.label,
                    button.custom_id.rsplit(":", 1)[-1],
                    button.style,
                )
                for button in row.children
            )
            for row in rows
        )

        self.assertEqual(actual, EXPECTED_ROWS)
        buttons = [
            item
            for item in panel.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(all(button.emoji is None for button in buttons))
        self.assertTrue(panel.is_persistent())

    def test_custom_icons_are_assigned_to_the_matching_buttons(self):
        icon_names = {
            icon_name
            for row in PANEL_BUTTON_ROWS
            for _, _, icon_name, _ in row
        } | {"sec_access", "sec_members", "sec_channel"}
        emojis = {
            name: cast(
                discord.Emoji,
                discord.PartialEmoji(name=f"kryo_{name}", id=index + 1),
            )
            for index, name in enumerate(icon_names)
        }

        with patch.dict(vc_icons._emojis, emojis):
            panel = JtcPanel(None)

        buttons = [
            item
            for item in panel.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        expected_names = [
            f"kryo_{icon_name}"
            for row in PANEL_BUTTON_ROWS
            for _, _, icon_name, _ in row
        ]
        self.assertEqual([button.emoji.name for button in buttons], expected_names)
        self.assertIn(
            f"<:kryo_sec_access:{emojis['sec_access'].id}>",
            panel.children[1].children[0].content,
        )


class JoinToCreatePanelCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_button_dispatches_to_its_existing_action(self):
        recorder = PanelActionRecorder()
        panel = JtcPanel(recorder)
        buttons = [
            item
            for item in panel.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        interaction = object()

        for button in buttons:
            await button.callback(interaction)

        expected_keys = [
            key
            for row in EXPECTED_ROWS
            for _, key, _ in row
        ]
        self.assertEqual(recorder.keys, expected_keys)
        handled_keys = set(USER_ACTIONS) | set(MODAL_ACTIONS) | {
            "lock",
            "unlock",
            "hide",
            "unhide",
            "permitall",
            "claim",
        }
        self.assertEqual(set(expected_keys), handled_keys)
