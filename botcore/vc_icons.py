from pathlib import Path
from typing import Optional

import discord


PREFIX = "kryo_"
ICON_DIR = Path(__file__).with_name("vc_icons")
_TEXT_FALLBACK = {
    "sec_access": "◇",
    "sec_members": "◈",
    "sec_channel": "▤",
}
_emojis: dict[str, discord.Emoji] = {}


async def setup_icons(bot: discord.Client) -> None:
    """Load the JTC application emojis before registering its persistent view."""
    try:
        existing = {
            emoji.name: emoji for emoji in await bot.fetch_application_emojis()
        }
    except discord.HTTPException as error:
        print(
            f"vc_icons: could not fetch application emojis "
            f"({type(error).__name__}: {error}); panel icons will use text fallbacks."
        )
        return

    _emojis.clear()
    for path in sorted(ICON_DIR.glob("*.png")):
        name = PREFIX + path.stem
        emoji = existing.get(name)
        if emoji is None:
            try:
                emoji = await bot.create_application_emoji(
                    name=name,
                    image=path.read_bytes(),
                )
            except discord.HTTPException as error:
                print(f"vc_icons: could not upload {name}: {error}")
                continue
        _emojis[path.stem] = emoji
    print(f"vc_icons: {len(_emojis)} JTC icons ready.")


def icon(name: str) -> Optional[discord.Emoji]:
    return _emojis.get(name)


def text(name: str) -> str:
    emoji = _emojis.get(name)
    if emoji is not None:
        return str(emoji)
    return _TEXT_FALLBACK.get(name, "•")
