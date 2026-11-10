import os
import platform
import random
import re
import json
import base64
from datetime import datetime, timezone
import unicodedata
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from botcore import ui


def support_url() -> str:
    return os.getenv("SUPPORT_SERVER_URL", "").strip()


DICE_RE = re.compile(r"(?:(\d{1,2})?d)?(\d{1,4})\Z", re.IGNORECASE)
EIGHT_BALL_ANSWERS = (
    "Yes.", "No.", "Probably.", "Probably not.", "Without a doubt.",
    "Ask again later.", "The outlook is unclear.", "Absolutely not.",
)


def parse_dice(expression: str) -> tuple[int, int]:
    match = DICE_RE.fullmatch(expression.strip())
    if match is None:
        raise ValueError("Use a dice expression like `d20` or `3d6`.")
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    if count < 1 or count > 100 or sides < 2 or sides > 10_000:
        raise ValueError("Roll between 1–100 dice, each with 2–10,000 sides.")
    return count, sides


def parse_choices(text: str) -> list[str]:
    choices = [choice.strip() for choice in re.split(r"[,|]", text) if choice.strip()]
    if len(choices) < 2:
        raise ValueError("Give at least two choices, separated by commas or `|`.")
    if len(choices) > 20:
        raise ValueError("Choose between 2 and 20 options.")
    if any(len(choice) > 100 for choice in choices):
        raise ValueError("Each option must be 100 characters or fewer.")
    return choices


def parse_color(value: str) -> int:
    text = value.strip().removeprefix("#").removeprefix("0x")
    if len(text) != 6 or re.fullmatch(r"[0-9a-fA-F]{6}", text) is None:
        raise ValueError("Use a 6-digit hex color, such as `#8f80f7`.")
    return int(text, 16)


def parse_snowflake(value: str) -> int:
    if not value.isdecimal():
        raise ValueError("A Discord ID must contain digits only.")
    snowflake = int(value)
    if not 0 < snowflake < 2**64:
        raise ValueError("Discord IDs must be between 1 and 2^64 − 1.")
    return snowflake


def transform_text(text: str, style: str) -> str:
    transforms = {
        "upper": str.upper,
        "lower": str.lower,
        "title": str.title,
        "sentence": str.capitalize,
        "swap": str.swapcase,
    }
    transform = transforms.get(style)
    if transform is None:
        raise ValueError("Choose `upper`, `lower`, `title`, `sentence` or `swap`.")
    return transform(text)


def announcement_embed(
    guild: discord.Guild,
    message: str,
    title: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title or None,
        description=message,
        color=discord.Color.orange(),
    )
    embed.set_author(
        name=f"{guild.name} Announcement",
        icon_url=guild.icon.url if guild.icon else None,
    )
    return embed


# ======================================================================= UTILITY
class UtilityCog(commands.Cog, name="Utility"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def avatar_embed(self, member: discord.abc.User) -> discord.Embed:
        emb = ui.info(f"Avatar of {member.display_name}", f"[Open full size]({member.display_avatar.url})")
        emb.set_image(url=member.display_avatar.url)
        return emb

    def userinfo_embed(self, member: discord.Member) -> discord.Embed:
        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        rows = [
            ("User", member.mention),
            ("ID", f"`{member.id}`"),
            ("Created", discord.utils.format_dt(member.created_at, "R")),
            ("Joined", discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "`Unknown`"),
            ("Top role", member.top_role.mention if not member.top_role.is_default() else "`None`"),
            ("Bot", "`yes`" if member.bot else "`no`"),
        ]
        emb = ui.panel("User Information", None, rows, thumbnail=member.display_avatar.url)
        emb.add_field(name=f"Roles ({len(roles)})", value=(" ".join(roles[:15]) or "`None`")[:1000], inline=False)
        return emb

    def serverinfo_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = [
            ("Server", f"**{guild.name}**"),
            ("ID", f"`{guild.id}`"),
            ("Owner", f"<@{guild.owner_id}>"),
            ("Created", discord.utils.format_dt(guild.created_at, "R")),
            ("Members", f"`{guild.member_count}`"),
            ("Channels", f"`{len(guild.text_channels)}` text · `{len(guild.voice_channels)}` voice"),
            ("Roles", f"`{len(guild.roles)}`"),
            ("Boosts", f"`{guild.premium_subscription_count}` (level `{guild.premium_tier}`)"),
        ]
        return ui.panel("Server Information", None, rows, thumbnail=guild.icon.url if guild.icon else None)

    def membercount_embed(self, guild: discord.Guild) -> discord.Embed:
        bots = sum(1 for member in guild.members if member.bot)
        total = guild.member_count or len(guild.members)
        rows = [("Total", f"`{total}`"), ("Humans", f"`{total - bots}`"), ("Bots", f"`{bots}`")]
        return ui.panel("Member Count", None, rows)

    @commands.command(name="avatar", aliases=["av"], brief="Show a member's avatar.", extras={"category": "Utility", "usage": ".avatar [@user]", "public": True})
    @commands.guild_only()
    async def avatar(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        await ctx.send(embed=self.avatar_embed(member or ctx.author))

    @commands.command(name="userinfo", aliases=["ui", "whois"], brief="Show information about a member.", extras={"category": "Utility", "usage": ".userinfo [@user]", "public": True})
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        await ctx.send(embed=self.userinfo_embed(member or ctx.author))

    @commands.command(name="serverinfo", aliases=["si"], brief="Show information about this server.", extras={"category": "Utility", "usage": ".serverinfo", "public": True})
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.serverinfo_embed(ctx.guild))

    @commands.command(name="membercount", aliases=["mc"], brief="Show the member count.", extras={"category": "Utility", "usage": ".membercount", "public": True})
    @commands.guild_only()
    async def membercount(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.membercount_embed(ctx.guild))

    @commands.hybrid_command(name="servericon", description="Show this server's icon", brief="Show this server's icon.", extras={"category": "Utility", "usage": ".servericon", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def servericon(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        if guild.icon is None:
            await ctx.send(embed=ui.warn("No Server Icon", "This server does not have a custom icon."))
            return
        embed = ui.info(f"{guild.name} · Server Icon", f"[Open full size]({guild.icon.url})")
        embed.set_image(url=guild.icon.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="serverbanner", description="Show this server's banner", brief="Show this server's banner.", extras={"category": "Utility", "usage": ".serverbanner", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def serverbanner(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        if guild.banner is None:
            await ctx.send(embed=ui.warn("No Server Banner", "This server does not have a custom banner."))
            return
        embed = ui.info(f"{guild.name} · Server Banner", f"[Open full size]({guild.banner.url})")
        embed.set_image(url=guild.banner.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="roleinfo", description="Show details about a server role", brief="Show details about a server role.", extras={"category": "Utility", "usage": ".roleinfo @role", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def roleinfo(self, ctx: commands.Context, role: discord.Role) -> None:
        permissions = [
            name.replace("_", " ").title()
            for name, enabled in role.permissions
            if enabled
        ]
        embed = ui.panel(
            f"Role Information · {role.name}",
            None,
            [
                ("Role ID", f"`{role.id}`"),
                ("Color", f"`#{role.color.value:06X}`"),
                ("Position", f"`{role.position}`"),
                ("Members in cache", f"`{len(role.members)}`"),
                ("Hoisted", "`Yes`" if role.hoist else "`No`"),
                ("Mentionable", "`Yes`" if role.mentionable else "`No`"),
                ("Managed by integration", "`Yes`" if role.managed else "`No`"),
                ("Permissions", ", ".join(permissions[:25]) or "`None`"),
            ],
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="channelinfo", description="Show details about a text channel", brief="Show details about a text channel.", extras={"category": "Utility", "usage": ".channelinfo [#channel]", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def channelinfo(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send(embed=ui.warn("Text Channel Required", "Use this command in, or provide, a text channel."))
            return
        embed = ui.panel(
            f"Channel Information · #{target.name}",
            None,
            [
                ("Channel ID", f"`{target.id}`"),
                ("Category", target.category.mention if target.category else "`None`"),
                ("Position", f"`{target.position}`"),
                ("Created", discord.utils.format_dt(target.created_at, "R")),
                ("Slowmode", f"`{target.slowmode_delay}` seconds"),
                ("NSFW", "`Yes`" if target.nsfw else "`No`"),
                ("Topic", target.topic or "`None`"),
            ],
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="permissions", description="Show a member's effective server permissions", brief="Show a member's effective server permissions.", extras={"category": "Utility", "usage": ".permissions [@member]", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def permissions(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        guild = ctx.guild
        if guild is None:
            return
        target = member or ctx.author
        permissions = target.guild_permissions
        enabled = [
            name.replace("_", " ").title()
            for name, value in permissions
            if value
        ]
        description = ", ".join(enabled) if enabled else "No server permissions."
        embed = ui.panel(
            f"Server Permissions · {target.display_name}",
            description[:4000],
            [("Administrator", "`Yes`" if permissions.administrator else "`No`")],
            thumbnail=target.display_avatar.url,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="roles", description="List server roles by position", brief="List server roles by position.", extras={"category": "Utility", "usage": ".roles", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def roles(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        entries = [
            f"{role.mention} · position {role.position} · {len(role.members)} cached"
            for role in reversed(guild.roles)
            if not role.is_default()
        ]
        description = "\n".join(entries) or "This server has no custom roles."
        embed = ui.info(
            f"Server Roles · {len(entries)}",
            description[:4000] + (f"\n…and {len(entries) - 40} more." if len(entries) > 40 else ""),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="channels", description="List this server's text and voice channels", brief="List text and voice channels.", extras={"category": "Utility", "usage": ".channels", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def channels(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        entries = [
            f"**{category.name}**\n" + "\n".join(
                f"・{channel.mention}"
                for channel in category.channels
                if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))
            )
            for category in guild.categories
        ]
        uncategorized = [
            channel for channel in guild.channels
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))
            and channel.category is None
        ]
        if uncategorized:
            entries.append("**Uncategorized**\n" + "\n".join(f"・{channel.mention}" for channel in uncategorized))
        description = "\n\n".join(entries) or "This server has no text or voice channels."
        embed = ui.info(f"Server Channels · {len(guild.channels)}", description[:4000])
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="inrole", description="List members with a selected role", brief="List members with a selected role.", extras={"category": "Utility", "usage": ".inrole @role", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def inrole(self, ctx: commands.Context, role: discord.Role) -> None:
        members = role.members
        names = [member.mention for member in members[:50]]
        description = "\n".join(names) or "No members with this role are currently in cache."
        if len(members) > len(names):
            description += f"\n…and {len(members) - len(names)} more."
        embed = ui.info(f"Members with {role.name} · {len(members)} cached", description)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="emojis", description="List this server's custom emojis", brief="List this server's custom emojis.", extras={"category": "Utility", "usage": ".emojis", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def emojis(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        entries = [f"{emoji} `:{emoji.name}:`" for emoji in guild.emojis]
        embed = ui.info(
            f"Server Emojis · {len(entries)}",
            ("\n".join(entries) or "This server has no custom emojis.")[:4000],
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="stickers", description="List this server's stickers", brief="List this server's stickers.", extras={"category": "Utility", "usage": ".stickers", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def stickers(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        entries = [
            f"**{sticker.name}** · {sticker.format} · `{sticker.id}`"
            for sticker in guild.stickers
        ]
        embed = ui.info(
            f"Server Stickers · {len(entries)}",
            ("\n".join(entries) or "This server has no custom stickers.")[:4000],
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="choose", description="Choose randomly from a list of options", brief="Choose randomly from a list of options.", extras={"category": "Utility", "usage": ".choose option one | option two", "public": True})
    async def choose(self, ctx: commands.Context, *, options: str) -> None:
        try:
            choices = parse_choices(options)
        except ValueError as error:
            await ctx.send(embed=ui.warn("Invalid Choices", str(error)))
            return
        await ctx.send(
            embed=ui.info("Random Choice", f"I choose: **{discord.utils.escape_markdown(random.choice(choices))}**"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="coinflip", description="Flip a coin", brief="Flip a coin.", extras={"category": "Utility", "usage": ".coinflip", "public": True})
    async def coinflip(self, ctx: commands.Context) -> None:
        await ctx.send(embed=ui.info("Coin Flip", random.choice(("Heads", "Tails"))))

    @commands.hybrid_command(name="roll", description="Roll dice, such as d20 or 3d6", brief="Roll dice such as d20 or 3d6.", extras={"category": "Utility", "usage": ".roll [NdS]", "public": True})
    async def roll(self, ctx: commands.Context, expression: str = "d20") -> None:
        try:
            count, sides = parse_dice(expression)
        except ValueError as error:
            await ctx.send(embed=ui.warn("Invalid Dice", str(error)))
            return
        results = [random.randint(1, sides) for _ in range(count)]
        details = ", ".join(map(str, results))
        await ctx.send(
            embed=ui.info(
                f"🎲 {count}d{sides} · Total {sum(results)}",
                f"Rolls: {details}" if len(details) <= 1000 else f"{count} dice rolled.",
            )
        )

    @commands.hybrid_command(name="eightball", description="Ask the magic 8-ball a question", brief="Ask the magic 8-ball a question.", extras={"category": "Utility", "usage": ".eightball <question>", "public": True})
    async def eightball(self, ctx: commands.Context, *, question: str) -> None:
        if not question.strip():
            await ctx.send(embed=ui.warn("Question Required", "Ask a question for the 8-ball."))
            return
        answer = random.choice(EIGHT_BALL_ANSWERS)
        await ctx.send(
            embed=ui.info(
                "🎱 Magic 8-Ball",
                f"**Question:** {discord.utils.escape_markdown(question[:500])}\n**Answer:** {answer}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="timestamp", description="Format a date and time as a Discord timestamp", brief="Format an ISO date as a Discord timestamp.", extras={"category": "Utility", "usage": ".timestamp 2026-12-31T18:00:00+05:30", "public": True})
    async def timestamp(self, ctx: commands.Context, *, date_text: str) -> None:
        try:
            value = datetime.fromisoformat(date_text.strip().replace("Z", "+00:00"))
        except ValueError:
            await ctx.send(
                embed=ui.warn(
                    "Invalid Date",
                    "Use ISO 8601, for example `2026-12-31T18:00:00+05:30`.",
                )
            )
            return
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        unix = int(value.timestamp())
        await ctx.send(
            embed=ui.info(
                "Discord Timestamp",
                f"Local date: `{discord.utils.escape_markdown(value.isoformat())}`\n"
                f"Full: `<t:{unix}:F>` · Relative: `<t:{unix}:R>`\n"
                f"Copy: ` <t:{unix}:F> `",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="joinedat", description="Show when a member joined this server", brief="Show when a member joined this server.", extras={"category": "Utility", "usage": ".joinedat [@member]", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def joinedat(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target = member or ctx.author
        if target.joined_at is None:
            await ctx.send(embed=ui.warn("Join Date Unavailable", "Discord has not provided this member's server join date."))
            return
        await ctx.send(
            embed=ui.info(
                f"Joined · {discord.utils.escape_markdown(target.display_name)}",
                f"Joined {ctx.guild.name} {discord.utils.format_dt(target.joined_at, 'F')} "
                f"({discord.utils.format_dt(target.joined_at, 'R')}).",
            )
        )

    @commands.hybrid_command(name="createdat", description="Show when a Discord account was created", brief="Show when a Discord account was created.", extras={"category": "Utility", "usage": ".createdat [@user]", "public": True})
    async def createdat(self, ctx: commands.Context, user: Optional[discord.User] = None) -> None:
        target = user or ctx.author
        await ctx.send(
            embed=ui.info(
                f"Account Created · {discord.utils.escape_markdown(target.name)}",
                f"Created {discord.utils.format_dt(target.created_at, 'F')} "
                f"({discord.utils.format_dt(target.created_at, 'R')}).",
                thumbnail=target.display_avatar.url,
            )
        )

    @commands.hybrid_command(name="toprole", description="Show a member's highest role", brief="Show a member's highest role.", extras={"category": "Utility", "usage": ".toprole [@member]", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def toprole(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target = member or ctx.author
        role = target.top_role
        value = "`@everyone` (no assigned roles)" if role.is_default() else f"{role.mention} · position `{role.position}`"
        await ctx.send(
            embed=ui.info(f"Highest Role · {discord.utils.escape_markdown(target.display_name)}", value),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="rolecount", description="Count custom roles in this server", brief="Count custom roles in this server.", extras={"category": "Utility", "usage": ".rolecount", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def rolecount(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        custom_roles = [role for role in guild.roles if not role.is_default()]
        managed_roles = sum(role.managed for role in custom_roles)
        await ctx.send(
            embed=ui.panel(
                "Role Counts",
                None,
                [
                    ("Custom roles", f"`{len(custom_roles)}`"),
                    ("Managed by integrations", f"`{managed_roles}`"),
                    ("Assignable custom roles", f"`{len(custom_roles) - managed_roles}`"),
                ],
            )
        )

    @commands.hybrid_command(name="channelcount", description="Count this server's text, voice, and category channels", brief="Count text, voice, and category channels.", extras={"category": "Utility", "usage": ".channelcount", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def channelcount(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        rows = [
            ("Text channels", f"`{len(guild.text_channels)}`"),
            ("Voice channels", f"`{len(guild.voice_channels)}`"),
            ("Stage channels", f"`{len(guild.stage_channels)}`"),
            ("Categories", f"`{len(guild.categories)}`"),
            ("Forum channels", f"`{sum(isinstance(channel, discord.ForumChannel) for channel in guild.channels)}`"),
        ]
        await ctx.send(embed=ui.panel("Channel Counts", None, rows))

    @commands.hybrid_command(name="humancount", description="Count cached human members in this server", brief="Count cached human members.", extras={"category": "Utility", "usage": ".humancount", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def humancount(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        cached_humans = sum(not member.bot for member in guild.members)
        await ctx.send(
            embed=ui.info(
                "Human Members",
                f"**{cached_humans}** human members in the local cache "
                f"(Discord server total: **{guild.member_count or 'unknown'}**).",
            )
        )

    @commands.hybrid_command(name="botcount", description="Count cached bot members in this server", brief="Count cached bot members.", extras={"category": "Utility", "usage": ".botcount", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def botcount(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        cached_bots = sum(member.bot for member in guild.members)
        await ctx.send(
            embed=ui.info(
                "Bot Members",
                f"**{cached_bots}** bot members in the local cache. "
                f"Cached count; Discord's member list may be larger.",
            )
        )

    @commands.hybrid_command(name="boosters", description="List members currently boosting this server", brief="List current server boosters.", extras={"category": "Utility", "usage": ".boosters", "public": True})
    @commands.guild_only()
    @app_commands.guild_only()
    async def boosters(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            return
        members = guild.premium_subscribers
        names = [member.mention for member in members[:50]]
        description = "\n".join(names) or "No current boosters are available in the member cache."
        if len(members) > len(names):
            description += f"\n…and {len(members) - len(names)} more."
        await ctx.send(
            embed=ui.info(f"Server Boosters · {len(members)} cached", description),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="colorinfo", description="Preview and describe a hexadecimal color", brief="Preview a six-digit hexadecimal color.", extras={"category": "Utility", "usage": ".colorinfo #8f80f7", "public": True})
    async def colorinfo(self, ctx: commands.Context, color: str) -> None:
        try:
            value = parse_color(color)
        except ValueError as error:
            await ctx.send(embed=ui.warn("Invalid Color", str(error)))
            return
        embed = discord.Embed(
            title="Color Preview",
            description=f"HEX `#{value:06X}` · RGB `{(value >> 16, (value >> 8) & 255, value & 255)}`",
            color=discord.Color(value),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="snowflake", description="Convert a Discord snowflake ID to its creation time", brief="Convert a Discord ID to its creation time.", extras={"category": "Utility", "usage": ".snowflake <discord_id>", "public": True})
    async def snowflake(self, ctx: commands.Context, snowflake_text: str) -> None:
        try:
            snowflake = parse_snowflake(snowflake_text)
            created_at = discord.utils.snowflake_time(snowflake)
        except (ValueError, OverflowError) as error:
            await ctx.send(embed=ui.warn("Invalid Discord ID", str(error)))
            return
        await ctx.send(
            embed=ui.info(
                "Discord ID Creation Time",
                f"Created {discord.utils.format_dt(created_at, 'F')} "
                f"({discord.utils.format_dt(created_at, 'R')}).",
            )
        )

    @commands.hybrid_command(name="textcase", description="Convert text to a selected letter case", brief="Convert text to a selected case.", extras={"category": "Utility", "usage": ".textcase upper|lower|title|sentence|swap <text>", "public": True})
    @app_commands.choices(style=[
        app_commands.Choice(name="UPPERCASE", value="upper"),
        app_commands.Choice(name="lowercase", value="lower"),
        app_commands.Choice(name="Title Case", value="title"),
        app_commands.Choice(name="Sentence case", value="sentence"),
        app_commands.Choice(name="sWAP cASE", value="swap"),
    ])
    async def textcase(self, ctx: commands.Context, style: str, *, text: str) -> None:
        if len(text) > 1800:
            await ctx.send(embed=ui.warn("Text Too Long", "Text case conversion is limited to 1,800 characters."))
            return
        try:
            result = transform_text(text, style)
        except ValueError as error:
            await ctx.send(embed=ui.warn("Invalid Case", str(error)))
            return
        await ctx.send(
            embed=ui.info("Text Case", f"```text\n{discord.utils.escape_markdown(result)}\n```"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="charcount", description="Count characters in a text", brief="Count characters in text.", extras={"category": "Utility", "usage": ".charcount <text>", "public": True})
    async def charcount(self, ctx: commands.Context, *, text: str) -> None:
        await ctx.send(
            embed=ui.info(
                "Character Count",
                f"**{len(text)}** Unicode code points · **{len(text.strip())}** excluding leading/trailing whitespace.",
            )
        )

    @commands.hybrid_command(name="wordcount", description="Count words and lines in text", brief="Count words, lines and characters.", extras={"category": "Utility", "usage": ".wordcount <text>", "public": True})
    async def wordcount(self, ctx: commands.Context, *, text: str) -> None:
        words = text.split()
        lines = text.splitlines()
        await ctx.send(
            embed=ui.panel(
                "Text Statistics",
                None,
                [
                    ("Words", f"`{len(words)}`"),
                    ("Lines", f"`{len(lines)}`"),
                    ("Characters", f"`{len(text)}`"),
                    ("Characters without spaces", f"`{sum(not character.isspace() for character in text)}`"),
                ],
            )
        )

    @commands.hybrid_command(name="jsonformat", description="Validate and pretty-print JSON", brief="Validate and format JSON.", extras={"category": "Utility", "usage": ".jsonformat <json>", "public": True})
    async def jsonformat(self, ctx: commands.Context, *, text: str) -> None:
        if len(text) > 4000:
            await ctx.send(embed=ui.warn("JSON Too Long", "JSON input is limited to 4,000 characters."))
            return
        try:
            formatted = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError as error:
            await ctx.send(
                embed=ui.warn(
                    "Invalid JSON",
                    f"{error.msg} at line {error.lineno}, column {error.colno}.",
                )
            )
            return
        if len(formatted) > 3900:
            await ctx.send(embed=ui.warn("Output Too Long", "Formatted JSON exceeds Discord's message limit."))
            return
        await ctx.send(
            embed=ui.info("Formatted JSON", f"```json\n{formatted}\n```"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="base64", description="Encode text or decode a Base64 string", brief="Encode or decode Base64 text.", extras={"category": "Utility", "usage": ".base64 encode|decode <text>", "public": True})
    @app_commands.choices(action=[
        app_commands.Choice(name="Encode", value="encode"),
        app_commands.Choice(name="Decode", value="decode"),
    ])
    async def base64(self, ctx: commands.Context, action: str, *, text: str) -> None:
        if len(text) > 5000:
            await ctx.send(embed=ui.warn("Input Too Long", "Base64 input is limited to 5,000 characters."))
            return
        try:
            if action == "encode":
                result = base64.b64encode(text.encode("utf-8")).decode("ascii")
            elif action == "decode":
                result = base64.b64decode(text, validate=True).decode("utf-8")
            else:
                await ctx.send(embed=ui.warn("Invalid Action", "Choose `encode` or `decode`."))
                return
        except (ValueError, UnicodeDecodeError) as error:
            await ctx.send(embed=ui.warn("Invalid Base64", f"Could not decode this input as UTF-8 Base64: {error}."))
            return
        if len(result) > 3900:
            await ctx.send(embed=ui.warn("Output Too Long", "Result exceeds Discord's message limit."))
            return
        await ctx.send(
            embed=ui.info(f"Base64 · {action.title()}", f"```text\n{discord.utils.escape_markdown(result)}\n```"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="urlencode", description="Percent-encode text for use in a URL", brief="Encode text for a URL.", extras={"category": "Utility", "usage": ".urlencode <text>", "public": True})
    async def urlencode_text(self, ctx: commands.Context, *, text: str) -> None:
        encoded = quote(text, safe="")
        if len(encoded) > 3900:
            await ctx.send(embed=ui.warn("Output Too Long", "URL-encoded output exceeds Discord's message limit."))
            return
        await ctx.send(
            embed=ui.info("URL Encoded Text", f"```text\n{discord.utils.escape_markdown(encoded)}\n```"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="urldecode", description="Decode percent-encoded URL text", brief="Decode URL-encoded text.", extras={"category": "Utility", "usage": ".urldecode <text>", "public": True})
    async def urldecode_text(self, ctx: commands.Context, *, text: str) -> None:
        decoded = unquote(text, errors="replace")
        await ctx.send(
            embed=ui.info("URL Decoded Text", f"```text\n{discord.utils.escape_markdown(decoded[:1800])}\n```"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="unicode", description="Show Unicode code points for a short string", brief="Inspect Unicode code points in text.", extras={"category": "Utility", "usage": ".unicode <text>", "public": True})
    async def unicode_info(self, ctx: commands.Context, *, text: str) -> None:
        if len(text) > 40:
            await ctx.send(embed=ui.warn("Text Too Long", "Unicode inspection is limited to 40 code points."))
            return
        rows = [
            f"`U+{ord(character):04X}` · {discord.utils.escape_markdown(character)} · "
            f"{unicodedata.name(character, 'UNNAMED CHARACTER')}"
            for character in text
        ]
        description = "\n".join(rows)
        if len(description) > 3900:
            await ctx.send(embed=ui.warn("Output Too Long", "Unicode details exceed Discord's message limit."))
            return
        await ctx.send(
            embed=ui.info("Unicode Code Points", description),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="announce", description="Post an announcement in a server text channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        message="Announcement text. User mentions are allowed; role and everyone mentions are blocked.",
        channel="Where to post it (defaults to this channel)",
        title="Optional announcement title",
    )
    async def slash_announce(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2000],
        channel: Optional[discord.TextChannel] = None,
        title: Optional[app_commands.Range[str, 1, 256]] = None,
    ) -> None:
        guild = interaction.guild
        destination = channel or interaction.channel
        if guild is None or not isinstance(destination, (discord.TextChannel, discord.Thread)):
            await ui.respond(
                interaction,
                ui.warn("Text Channel Required", "Run this command in a text channel or choose a destination channel."),
                ephemeral=True,
            )
            return
        bot_member = guild.me
        if bot_member is None:
            await ui.respond(interaction, ui.error("Not Ready", "The bot's server permissions are unavailable."), ephemeral=True)
            return
        permissions = destination.permissions_for(bot_member)
        can_send = permissions.send_messages_in_threads if isinstance(destination, discord.Thread) else permissions.send_messages
        if not can_send or not permissions.embed_links:
            await ui.respond(
                interaction,
                ui.error("Missing Permissions", f"I need permission to send messages and embeds in {destination.mention}."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await destination.send(
                embed=announcement_embed(guild, message, title),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.Forbidden as error:
            print(f"Announcement send forbidden in guild {guild.id}, channel {destination.id}: {error}")
            await ui.respond(
                interaction,
                ui.error("Announcement Not Sent", f"Discord denied permission to post in {destination.mention}."),
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            print(f"Announcement send failed in guild {guild.id}, channel {destination.id}: {error}")
            await ui.respond(
                interaction,
                ui.error("Announcement Not Sent", "Discord could not deliver the announcement. Please try again."),
                ephemeral=True,
            )
            return

        await ui.respond(
            interaction,
            ui.success("Announcement Posted", f"Your announcement was posted in {destination.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="avatar", description="Show a member's avatar")
    @app_commands.guild_only()
    async def s_avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        await ui.respond(interaction, self.avatar_embed(member or interaction.user))

    @app_commands.command(name="userinfo", description="Show information about a member")
    @app_commands.guild_only()
    async def s_userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        await ui.respond(interaction, self.userinfo_embed(member or interaction.user))

    @app_commands.command(name="serverinfo", description="Show information about this server")
    @app_commands.guild_only()
    async def s_serverinfo(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.serverinfo_embed(interaction.guild))


# ======================================================================= OTHER
class OtherCog(commands.Cog, name="Other"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def ping_embed(self, guild: discord.Guild | None = None) -> discord.Embed:
        gateway_ms = self.bot.latency * 1000
        gateway = f"**{round(gateway_ms)} ms**" if gateway_ms != float("inf") else "**Measuring...**"
        loop_avg = getattr(self.bot, "loop_lag_avg_ms", 0.0)
        loop_max = getattr(self.bot, "loop_lag_max_ms", 0.0)
        rows = [
            f"Discord Gateway: {gateway} (heartbeat round trip)",
            f"Event loop lag: **{round(loop_avg)} ms** avg / **{round(loop_max)} ms** peak",
        ]
        player = guild.voice_client if guild is not None else None
        if isinstance(player, wavelink.Player):
            voice_ping = player.ping
            value = f"**{voice_ping} ms**" if voice_ping >= 0 else "**Measuring...**"
            rows.append(f"Music Voice: {value}")
        else:
            rows.append("Music Voice: **Not connected**")
        return ui.info("Pong", "\n".join(rows))

    def botinfo_embed(self) -> discord.Embed:
        users = sum(guild.member_count or 0 for guild in self.bot.guilds)
        gateway_ms = self.bot.latency * 1000
        gateway = f"{round(gateway_ms)} ms" if gateway_ms != float("inf") else "n/a"
        rows = [
            ("Servers", f"`{len(self.bot.guilds)}`"),
            ("Users", f"`{users}`"),
            ("Prefix commands", f"`{len(list(self.bot.walk_commands()))}`"),
            ("Gateway RTT", f"`{gateway}`"),
            ("Loop lag", f"`{round(getattr(self.bot, 'loop_lag_avg_ms', 0.0))} ms` avg / `{round(getattr(self.bot, 'loop_lag_max_ms', 0.0))} ms` peak"),
            ("discord.py", f"`{discord.__version__}`"),
            ("Python", f"`{platform.python_version()}`"),
        ]
        return ui.panel(f"{ui.BRAND} Information", "Security, music and server management in one bot.", rows, thumbnail=self.bot.user.display_avatar.url if self.bot.user else None)

    def invite_embed(self) -> discord.Embed:
        url = discord.utils.oauth_url(self.bot.user.id, permissions=discord.Permissions(administrator=True), scopes=("bot", "applications.commands"))
        return ui.info("Invite Me", f"[Click here to add {ui.BRAND} to your server]({url})")

    def support_embed(self) -> discord.Embed:
        url = support_url()
        if not url:
            return ui.warn("Support Server", "The support server link is not configured yet.")
        return ui.info("Support Server", f"[Join our support server]({url})")

    @commands.command(name="ping", brief="Show the bot latency.", extras={"category": "Other", "usage": ".ping", "public": True})
    async def ping(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.ping_embed(ctx.guild))

    @commands.command(name="botinfo", aliases=["about", "stats"], brief="Show information about the bot.", extras={"category": "Other", "usage": ".botinfo", "public": True})
    async def botinfo(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.botinfo_embed())

    @commands.command(name="invite", brief="Get the bot invite link.", extras={"category": "Other", "usage": ".invite", "public": True})
    async def invite(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.invite_embed())

    @commands.command(name="support", brief="Get the support server link.", extras={"category": "Other", "usage": ".support", "public": True})
    async def support(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.support_embed())

    @app_commands.command(name="ping", description="Show the bot latency")
    async def s_ping(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.ping_embed(interaction.guild))

    @app_commands.command(name="botinfo", description="Show information about the bot")
    async def s_botinfo(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.botinfo_embed())


# ======================================================================= NO PREFIX
class NoPrefixCog(commands.Cog, name="No Prefix"):
    """Bot owner only: listed users can run commands without typing the prefix."""

    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def ids(self) -> list[int]:
        return self.state.setdefault("noprefix", [])

    def _animated_status_icon(self, ctx: commands.Context, key: str) -> str:
        guild = ctx.guild
        if guild is None:
            return ""
        mass_mention_cog = self.bot.get_cog("MassMentionCog")
        if mass_mention_cog is None:
            return ""
        return getattr(mass_mention_cog, "emojis", {}).get(guild.id, {}).get(key, "")

    @commands.group(name="noprefix", invoke_without_command=True, brief="Bot owner: let users run commands without a prefix.",
                    extras={"category": "No Prefix", "usage": ".noprefix add|remove|list"})
    @commands.is_owner()
    async def noprefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing())

    @noprefix.command(name="add", brief="Give a user no-prefix access: .noprefix add @user")
    @commands.is_owner()
    async def np_add(self, ctx: commands.Context, user: discord.User) -> None:
        if user.id in self.ids():
            icon = self._animated_status_icon(ctx, "GLOBE")
            title = f"{icon}  NO-PREFIX · ALREADY ACTIVE" if icon else "NO-PREFIX · ALREADY ACTIVE"
            await ctx.send(
                embed=ui.warn(
                    title,
                    f"{user.mention}\n\n"
                    "**Access is already active.**\n"
                    "This member can use KRYO commands without a prefix.",
                )
            )
            return
        self.ids().append(user.id)
        self.save()
        await ctx.send(embed=ui.success("No Prefix Added", f"{user.mention} can now run commands without the prefix."))

    @noprefix.command(name="remove", brief="Remove no-prefix access: .noprefix remove @user")
    @commands.is_owner()
    async def np_remove(self, ctx: commands.Context, user: discord.User) -> None:
        if user.id not in self.ids():
            await ctx.send(embed=ui.warn("Not Listed", f"{user.mention} does not have no-prefix access."))
            return
        self.ids().remove(user.id)
        self.save()
        await ctx.send(embed=ui.success("No Prefix Removed", f"{user.mention} must use the prefix again."))

    @noprefix.command(name="list", brief="Show everyone with no-prefix access.")
    @commands.is_owner()
    async def np_list(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing())

    def listing(self) -> discord.Embed:
        text = "\n".join(f"{ui.SPIDER} <@{user_id}> (`{user_id}`)" for user_id in self.ids()) or "Nobody has no-prefix access yet."
        return ui.info("No Prefix Users", text[:4000])
