"""Category icons for the help menu.

The PNG files in assets/emojis/ are uploaded ONCE as *application emojis* of the bot.
Application emojis work in every server (and in dropdowns) without "Use External Emojis"
and without keeping an emoji server. On the next starts the bot only reads them back.
"""
from pathlib import Path
from typing import Optional

import discord

from botcore import ui

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "emojis"

# help category -> png file name (without .png)
FILES = {
    "Brand": "brand",
    "Anti Nuke": "antinuke",
    "Auto Mod": "automod",
    "Role Config": "roleconfig",
    "Moderation": "moderation",
    "Smart Moderation": "smartmod",
    "Join To Create": "j2c",
    "No Prefix": "noprefix",
    "Other": "other",
    "Auto Role": "autorole",
    "Welcomer": "welcomer",
    "Self Roles": "selfroles",
    "Utility": "utility",
    "Voice": "voice",
    "Music": "music",  # optional: drop a music.png into assets/emojis to get a Music icon
}

_cache: dict[str, str] = {}


def icon(category: str) -> str:
    """Emoji text for a category. Falls back to the spider emoji if the icon is not ready."""
    return _cache.get(category) or ui.SPIDER


async def sync(bot: discord.Client) -> None:
    """Upload missing icons and remember every emoji. Safe to call on every start."""
    existing = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}
    created = 0
    for category, stem in FILES.items():
        name = f"kx_{stem}"
        emoji: Optional[discord.Emoji] = existing.get(name)
        if emoji is None:
            path = ASSET_DIR / f"{stem}.png"
            if not path.is_file():
                continue
            try:
                emoji = await bot.create_application_emoji(name=name, image=path.read_bytes())
                created += 1
            except discord.HTTPException as error:
                print(f"Emojis: could not upload {path.name} ({type(error).__name__}: {error})")
                continue
        _cache[category] = str(emoji)
    print(f"Emojis: {len(_cache)} category icons ready ({created} newly uploaded).")
