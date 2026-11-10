from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import discord


FOOTER_TEXT = "KRYO • Security & Music"
EMOJI_FALLBACKS = {
    "GAVEL": "🔨",
    "UNLOCK": "🔓",
    "PHOTOSCAN": "🖼️",
    "CAPS": "🔠",
    "GLOBE": "🌐",
    "SHIELD": "🛡️",
    "ARROW_RED": "➤",
    "ARROW_GREEN": "➤",
    "ARROW_PURPLE": "➤",
    "CHEVRONS_RED": "›››",
    "CHEVRONS_GREEN": "›››",
    "CHEVRONS_PURPLE": "›››",
}


def moderation_emojis_for(bot: Any, guild_id: int) -> dict[str, str]:
    values = dict(EMOJI_FALLBACKS)
    cog = bot.get_cog("MassMentionCog")
    values.update(getattr(cog, "emojis", {}).get(guild_id, {}))
    return values


def _set_icon_title(
    embed: discord.Embed,
    title: str,
    emoji_key: str,
    emojis: Mapping[str, str],
    fallback: str,
) -> None:
    icon_url = emojis.get(f"{emoji_key}_URL")
    if icon_url:
        embed.set_author(name=title, icon_url=icon_url)
    else:
        embed.title = f"{emojis.get(emoji_key, fallback)} {title}"


def _finish(
    embed: discord.Embed,
    bot_avatar_url: str | None,
) -> discord.Embed:
    if bot_avatar_url:
        embed.set_footer(text=FOOTER_TEXT, icon_url=bot_avatar_url)
    else:
        embed.set_footer(text=FOOTER_TEXT)
    return embed


def _embed(
    *,
    title: str,
    emoji_key: str,
    fallback: str,
    color: int,
    description: str,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        description=description,
        color=discord.Colour(color),
        timestamp=datetime.now(timezone.utc),
    )
    _set_icon_title(embed, title, emoji_key, emojis, fallback)
    return _finish(embed, bot_avatar_url)


def build_moderation_action_dm(
    *,
    guild_name: str,
    action: str,
    reason: str,
    moderator: str | None,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    lowered = action.lower()
    if "unbanned" in lowered:
        title, icon_key, fallback, color = "You were unbanned", "UNLOCK", "🔓", 0x23A55A
        arrow_key, chevrons_key = "ARROW_GREEN", "CHEVRONS_GREEN"
    elif "timed out" in lowered:
        title, icon_key, fallback, color = "You were timed out", "SHIELD", "🛡️", 0xF23F43
        arrow_key, chevrons_key = "ARROW_RED", "CHEVRONS_RED"
    else:
        title = f"You were {lowered}"
        icon_key, fallback, color = "GAVEL", "🔨", 0xF23F43
        arrow_key, chevrons_key = "ARROW_RED", "CHEVRONS_RED"

    arrow = emojis.get(arrow_key, EMOJI_FALLBACKS[arrow_key])
    chevrons = emojis.get(chevrons_key, EMOJI_FALLBACKS[chevrons_key])
    clean_reason = discord.utils.escape_mentions(reason or "No reason provided")
    clean_reason = discord.utils.escape_markdown(clean_reason).replace("\n", " ")[:1000]
    safe_guild = discord.utils.escape_markdown(discord.utils.escape_mentions(guild_name))
    safe_moderator = (
        discord.utils.escape_markdown(discord.utils.escape_mentions(moderator))
        if moderator else None
    )
    rows = [
        f"{arrow} **Server** — {safe_guild}",
        f"{arrow} **Reason** — {clean_reason}",
    ]
    if safe_moderator:
        rows.append(f"{arrow} **Moderator** — {safe_moderator}")
    rows.append(f"> {chevrons} Contact server staff if you need help.")
    return _embed(
        title=title,
        emoji_key=icon_key,
        fallback=fallback,
        color=color,
        description="-# KRYO moderation notice\n" + "\n".join(rows),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_warning_dm(
    *,
    guild_name: str,
    reason: str,
    total_warnings: int,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    arrow = emojis.get("ARROW_PURPLE", EMOJI_FALLBACKS["ARROW_PURPLE"])
    chevrons = emojis.get("CHEVRONS_PURPLE", EMOJI_FALLBACKS["CHEVRONS_PURPLE"])
    safe_guild = discord.utils.escape_markdown(discord.utils.escape_mentions(guild_name))
    safe_reason = discord.utils.escape_markdown(
        discord.utils.escape_mentions(reason or "No reason provided")
    ).replace("\n", " ")[:1000]
    return _embed(
        title="You received a warning",
        emoji_key="SHIELD",
        fallback="🛡️",
        color=0xF0B232,
        description=(
            "-# Moderation warning\n"
            f"{arrow} **Server** — {safe_guild}\n"
            f"{arrow} **Reason** — {safe_reason}\n"
            f"{arrow} **Total warnings** — {total_warnings}\n"
            f"> {chevrons} Please follow the server rules."
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_notice_dm(
    *,
    title: str,
    message: str,
    emojis: Mapping[str, str],
    icon_key: str = "SHIELD",
    arrow_key: str = "ARROW_PURPLE",
    chevrons_key: str = "CHEVRONS_PURPLE",
    color: int = 0x5865F2,
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    arrow = emojis.get(arrow_key, EMOJI_FALLBACKS.get(arrow_key, "➤"))
    chevrons = emojis.get(chevrons_key, EMOJI_FALLBACKS.get(chevrons_key, "›››"))
    safe_message = discord.utils.escape_mentions(message)
    return _embed(
        title=title,
        emoji_key=icon_key,
        fallback=EMOJI_FALLBACKS.get(icon_key, "🛡️"),
        color=color,
        description=(
            f"-# {FOOTER_TEXT}\n"
            f"{arrow} **Notice** — {safe_message}\n"
            f"> {chevrons} KRYO • Security & Music"
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_ban_embed(
    *,
    username: str,
    reason: str | None,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    arrow = emojis.get("ARROW_RED", "➤")
    chevrons = emojis.get("CHEVRONS_RED", "›››")
    safe_name = discord.utils.escape_markdown(username)
    safe_reason = discord.utils.escape_mentions(reason or "No reason given").replace("\n", " ")[:1000]
    return _embed(
        title="Member banned",
        emoji_key="GAVEL",
        fallback="🔨",
        color=0xF23F43,
        description=(
            "-# Permanently removed\n"
            f"**{safe_name}** was banned.\n"
            f"{arrow} **Reason** — {safe_reason}\n"
            f"{arrow} **Action** — Banned from server\n"
            f"> {chevrons} Ban enforced"
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_unban_embed(
    *,
    username: str,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    arrow = emojis.get("ARROW_GREEN", "➤")
    chevrons = emojis.get("CHEVRONS_GREEN", "›››")
    safe_name = discord.utils.escape_markdown(username)
    return _embed(
        title="Member unbanned",
        emoji_key="UNLOCK",
        fallback="🔓",
        color=0x23A55A,
        description=(
            "-# Access restored\n"
            f"**{safe_name}** was unbanned.\n"
            f"{arrow} **Action** — Ban lifted\n"
            f"{arrow} **Status** — Can rejoin the server\n"
            f"> {chevrons} Ban removed"
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_promo_removed_embed(
    *,
    user_id: int,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    mention = f"<@{user_id}>"
    arrow = emojis.get("ARROW_PURPLE", "➤")
    chevrons = emojis.get("CHEVRONS_PURPLE", "›››")
    return _embed(
        title="Promotional image removed",
        emoji_key="PHOTOSCAN",
        fallback="🖼️",
        color=0x7C3AED,
        description=(
            "-# Scan complete\n"
            f"{mention}, gambling/casino promotional images are not allowed here.\n"
            f"{arrow} **User** — {mention}\n"
            f"{arrow} **Found** — Gambling or casino promo\n"
            f"{arrow} **Action** — Image removed\n"
            f"> {chevrons} Promotions are blocked in this server."
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_caps_embed(
    *,
    user_id: int,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    mention = f"<@{user_id}>"
    arrow = emojis.get("ARROW_PURPLE", "➤")
    chevrons = emojis.get("CHEVRONS_PURPLE", "›››")
    return _embed(
        title="Too many capital letters",
        emoji_key="CAPS",
        fallback="🔠",
        color=0xC026D3,
        description=(
            "-# Caps filter active\n"
            f"{mention}, please do not write in all caps.\n"
            f"{arrow} **User** — {mention}\n"
            f"{arrow} **Found** — All-caps message\n"
            f"{arrow} **Action** — Message removed\n"
            f"> {chevrons} Please type in normal case."
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )


def build_link_blocked_embed(
    *,
    user_id: int,
    emojis: Mapping[str, str],
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    mention = f"<@{user_id}>"
    arrow = emojis.get("ARROW_PURPLE", "➤")
    chevrons = emojis.get("CHEVRONS_PURPLE", "›››")
    return _embed(
        title="Links are not allowed",
        emoji_key="GLOBE",
        fallback="🌐",
        color=0xA855F7,
        description=(
            "-# Link filter active\n"
            f"{mention}, posting links is disabled in this server.\n"
            f"{arrow} **User** — {mention}\n"
            f"{arrow} **Found** — Link in message\n"
            f"{arrow} **Action** — Message removed\n"
            f"> {chevrons} Need to share something? Ask a moderator."
        ),
        emojis=emojis,
        bot_avatar_url=bot_avatar_url,
    )
