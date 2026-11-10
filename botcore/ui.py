"""Shared embed helpers so every reply of the bot looks consistent and professional."""
import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

BRAND = "KRYO"

# Plain-text fallback for components that still need a small inline icon.
SPIDER_EMOJI = "🕸️"
FALLBACK_SPIDER = SPIDER_EMOJI
FALLBACK_SECONDARY = "🕸️"

SPIDER = SPIDER_EMOJI
SECONDARY_SPIDER = FALLBACK_SECONDARY
EMBED_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "kryo-embed"
EMBED_ICON_ASSETS = {
    "kx_ui_success": "check_sparkle_purple.gif",
    "kx_ui_error": "gavel_strike_red.gif",
    "kx_ui_warning": "shield_pulse.gif",
    "kx_ui_info": "globe_orbit_purple.gif",
    "kx_ui_music": "arrow_purple.gif",
}
EMBED_ICON_BY_KIND = {
    "success": "kx_ui_success",
    "error": "kx_ui_error",
    "warn": "kx_ui_warning",
    "info": "kx_ui_info",
    "gold": "kx_ui_success",
    "music": "kx_ui_music",
    "security": "kx_ui_warning",
}
EMBED_ICON_URLS: dict[str, str] = {}
EMBED_EMOJI_MENTIONS: dict[str, str] = {}

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
WELCOME_EMOJI_MAP: dict[str, str] = {}
WELCOME_EMOJI_MENTION = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{17,20})>"
)
ANIMATED_EMOJI_MENTION = re.compile(r"<a:[A-Za-z0-9_]{2,32}:\d{17,20}>")


async def setup_emojis(bot: discord.Client, state: dict[str, Any] | None = None) -> None:
    """Upload shared icons and welcome emoji copies as application emojis.

    SPIDER uses the configured shared emoji above and is never replaced by a
    generated application emoji. Welcome emojis are copied from their public
    Discord CDN images so Discord can render them without external-emoji access.
    """
    import botcore.ui as ui

    env_secondary = os.getenv("SECONDARY_SPIDER_EMOJI", "").strip()
    if env_secondary:
        ui.SECONDARY_SPIDER = env_secondary

    try:
        application_emojis = await bot.fetch_application_emojis()
    except discord.HTTPException as error:
        print(f"Emojis: could not fetch application emojis ({type(error).__name__}: {error})")
        application_emojis = []
    existing = {emoji.name: emoji for emoji in application_emojis}

    ui.EMBED_ICON_URLS.clear()
    ui.EMBED_EMOJI_MENTIONS.clear()
    for name, filename in ui.EMBED_ICON_ASSETS.items():
        emoji = existing.get(name)
        if emoji is None:
            path = ui.EMBED_ICON_DIR / filename
            try:
                emoji = await bot.create_application_emoji(
                    name=name,
                    image=path.read_bytes(),
                )
            except (discord.HTTPException, OSError) as error:
                print(f"Emojis: could not upload {path.name} ({type(error).__name__}: {error})")
                continue
        url = getattr(emoji, "url", None)
        if url is not None:
            ui.EMBED_ICON_URLS[name] = str(url)
        mention = str(emoji)
        if ui.WELCOME_EMOJI_MENTION.fullmatch(mention):
            ui.EMBED_EMOJI_MENTIONS[name] = mention

    secondary_path = ICON_DIR / "spider_secondary.png"

    if not env_secondary and secondary_path.exists():
        emoji = existing.get("spider_secondary")
        if emoji is None:
            try:
                uploaded = await bot.create_application_emoji(
                    name="spider_secondary",
                    image=secondary_path.read_bytes(),
                )
                emoji = uploaded
            except (discord.HTTPException, OSError) as error:
                print(f"Emojis: could not upload {secondary_path.name} ({type(error).__name__}: {error})")
                emoji = None
        if emoji:
            ui.SECONDARY_SPIDER = str(emoji)

    ui.WELCOME_EMOJI_MAP.clear()
    welcome_emojis: dict[str, tuple[str, bool]] = {}
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    if isinstance(settings, dict):
        for guild_settings in settings.values():
            if not isinstance(guild_settings, dict):
                continue
            welcome = guild_settings.get("welcome", {})
            if not isinstance(welcome, dict):
                continue
            for key in ("message", "leave_message"):
                message = welcome.get(key, "")
                if not isinstance(message, str):
                    continue
                for match in WELCOME_EMOJI_MENTION.finditer(message):
                    welcome_emojis.setdefault(
                        match.group("id"),
                        (match.group("name"), bool(match.group("animated"))),
                    )

    missing = {
        emoji_id: details
        for emoji_id, details in welcome_emojis.items()
        if f"kx_welcome_{emoji_id}" not in existing
    }
    for emoji_id, (name, _animated) in welcome_emojis.items():
        application_emoji = existing.get(f"kx_welcome_{emoji_id}")
        if application_emoji is not None:
            ui.WELCOME_EMOJI_MAP[emoji_id] = str(application_emoji)

    if missing:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for emoji_id, (name, animated) in missing.items():
                extension = "gif" if animated else "png"
                url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}?size=128&quality=lossless"
                try:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        image = await response.read()
                    application_emoji = await bot.create_application_emoji(
                        name=f"kx_welcome_{emoji_id}",
                        image=image,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException, OSError) as error:
                    print(
                        f"Emojis: could not prepare welcome emoji {name} ({emoji_id}): "
                        f"{type(error).__name__}: {error}"
                    )
                    continue
                ui.WELCOME_EMOJI_MAP[emoji_id] = str(application_emoji)
                print(f"Emojis: copied welcome emoji {name} ({emoji_id}) as an application emoji.")

    ui.ICONS = {kind: ui.SPIDER for kind in ui.COLORS}

COLORS = {
    "success": 0x2ECC71,
    "error": 0xE74C3C,
    "warn": 0xF39C12,
    "info": 0x5865F2,
    "gold": 0xF1C40F,
    "music": 0x9B59B6,
    "security": 0x1ABC9C,
}

ICONS = {kind: SPIDER for kind in COLORS}


def make(kind: str, title: str, description: str | None = None, footer: str | None = None) -> discord.Embed:
    has_animated_icon = bool(ANIMATED_EMOJI_MENTION.search(title))
    emb = discord.Embed(
        title=title if has_animated_icon else f"✧ {title}",
        description=description,
        color=COLORS.get(kind, COLORS["info"]),
        timestamp=datetime.now(timezone.utc),
    )
    if not has_animated_icon:
        icon_name = EMBED_ICON_BY_KIND.get(kind)
        icon_url = EMBED_ICON_URLS.get(icon_name) if icon_name else None
        if icon_url:
            emb.set_thumbnail(url=icon_url)
    emb.set_footer(text=footer or f"{BRAND} • Security & Music")
    return emb


def success(title: str, description: str | None = None) -> discord.Embed:
    return make("success", title, description)


def error(title: str, description: str | None = None) -> discord.Embed:
    return make("error", title, description)


def warn(title: str, description: str | None = None) -> discord.Embed:
    return make("warn", title, description)


def info(title: str, description: str | None = None) -> discord.Embed:
    return make("info", title, description)


def announcement(
    guild: discord.Guild,
    title: str,
    description: str | None,
    color: str = "#F39C12",
    image_url: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=title or "Server Announcement",
        description=description,
        color=discord.Color(int(color[1:], 16)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name=f"{BRAND}  •  {guild.name}",
        icon_url=guild.icon.url if guild.icon else None,
    )
    embed.set_footer(text="OFFICIAL SERVER ANNOUNCEMENT  •  KRYO")
    if image_url:
        embed.set_image(url=image_url)
    return embed


def gold(title: str, description: str | None = None) -> discord.Embed:
    return make("gold", title, description)


def music(title: str, description: str | None = None) -> discord.Embed:
    return make("music", title, description)


def security(title: str, description: str | None = None) -> discord.Embed:
    return make("security", title, description)


def panel(title, intro=None, rows=(), notes=(), thumbnail=None, kind="security", footer=None):
    """Clean arranged panel: intro text, one Spider-Man row per item, quoted notes at the end.

    `rows` accepts either `(name, value)` pairs or ready-made strings, so a
    caller that has already formatted its own line does not have to split it.
    """
    parts = []
    if intro:
        parts.append(intro)
    if rows:
        if parts:
            parts.append("")
        for row in rows:
            if isinstance(row, str):
                parts.append(row)
                continue
            name, value = row
            parts.append(f"{SPIDER} **{name}**" + (f" : {value}" if value else ""))
    for note in notes:
        parts.append("")
        parts.append(f"> **{note}**")
    emb = make(kind, title, "\n".join(parts), footer)
    if thumbnail:
        emb.set_thumbnail(url=thumbnail)
    return emb


def is_problem(emb: discord.Embed) -> bool:
    """True for error / warning embeds (used to make those replies private)."""
    return emb.color is not None and emb.color.value in (COLORS["error"], COLORS["warn"])


async def respond(interaction: discord.Interaction, emb: discord.Embed, *, ephemeral: bool = False, view=None) -> None:
    """Send an embed as the first reply or as a follow-up, whichever is still possible."""
    kwargs = {"embed": emb, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)
