from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

import discord


def build_mass_mention_embed(
    *,
    user: discord.abc.User,
    level: int,
    max_level: int,
    tried: list[str],
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    level = max(1, min(level, max_level))
    arrow = emojis.get("ARROW", "➤")
    chevrons = emojis.get("CHEVRONS", "›››")
    shield = emojis.get("SHIELD", "🛡️")
    shield_url = emojis.get("SHIELD_URL")
    filled = "🟥" if level == max_level else "🟧"
    bar = filled * level + "⬛" * (max_level - level)
    tried_value = " · ".join(f"`{mention}`" for mention in tried)

    embed = discord.Embed(
        description=(
            "Auto-mod · Security system\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Mass mentions are not allowed in this server.\n\n"
            f"{arrow} **User** — {user.mention}\n"
            f"{arrow} **Tried** — {tried_value}\n"
            f"{arrow} **Action** — Message removed\n\n"
            f"{chevrons} **Warning level** — **{level} / {max_level}**\n"
            f"{bar}\n"
            "Warn · Mute · Kick · Ban"
        ),
        color=discord.Colour(0xF23F43),
        timestamp=datetime.now(timezone.utc),
    )
    if shield_url:
        embed.set_author(name="Mass mention blocked", icon_url=shield_url)
    else:
        embed.set_author(name=f"{shield} Mass mention blocked")
    if level == max_level:
        danger = "> 🔨 You have been banned for repeated mass mentions."
    else:
        danger = f"> 🔨 The next {max_level - level} mentions will result in a permanent ban."
    embed.description += f"\n\n{danger}"
    avatar = user.display_avatar.replace(size=128).url
    embed.set_thumbnail(url=avatar)
    if bot_avatar_url:
        embed.set_footer(text="KRYO • Security & Music", icon_url=bot_avatar_url)
    else:
        embed.set_footer(text="KRYO • Security & Music")
    return embed
