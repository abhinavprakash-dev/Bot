from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

import discord


def _footer(embed: discord.Embed, bot_avatar_url: str | None) -> discord.Embed:
    if bot_avatar_url:
        embed.set_footer(text="KRYO • Security & Music", icon_url=bot_avatar_url)
    else:
        embed.set_footer(text="KRYO • Security & Music")
    return embed


def build_not_whitelisted_embed(
    *,
    user: discord.abc.User,
    category: str,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    lock = emojis.get("LOCK", "🔒")
    lock_url = emojis.get("LOCK_URL")
    embed = discord.Embed(
        title="Not whitelisted",
        description=f"-# Access denied\n{user.mention} is not whitelisted in **{category}**.",
        color=discord.Colour(0x8B5CF6),
        timestamp=datetime.now(timezone.utc),
    )
    if lock_url:
        embed.set_author(name="Not whitelisted", icon_url=lock_url)
    else:
        embed.title = f"{lock} Not whitelisted"
    return _footer(embed, bot_avatar_url)


def build_whitelist_updated_embed(
    *,
    user: discord.abc.User,
    category: str,
    action: str,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    check = emojis.get("CHECK", "✅")
    check_url = emojis.get("CHECK_URL")
    verb = "added to" if action == "added" else "removed from"
    embed = discord.Embed(
        title="Whitelist updated",
        description=(
            f"{user.mention} was {verb} the whitelist for **{category}**.\n"
            "> Whitelisted members skip the chat filters of that category, but "
            "they are still punished if they try to nuke the server."
        ),
        color=discord.Colour(0xA855F7),
        timestamp=datetime.now(timezone.utc),
    )
    if check_url:
        embed.set_author(name="Whitelist updated", icon_url=check_url)
    else:
        embed.title = f"{check} Whitelist updated"
    return _footer(embed, bot_avatar_url)


def build_already_whitelisted_embed(
    *,
    user: discord.abc.User,
    category: str,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    check = emojis.get("CHECK", "✅")
    check_url = emojis.get("CHECK_URL")
    embed = discord.Embed(
        title="Already whitelisted",
        description=f"{user.mention} is already whitelisted in **{category}**.",
        color=discord.Colour(0xA855F7),
        timestamp=datetime.now(timezone.utc),
    )
    if check_url:
        embed.set_author(name="Already whitelisted", icon_url=check_url)
    else:
        embed.title = f"{check} Already whitelisted"
    return _footer(embed, bot_avatar_url)
