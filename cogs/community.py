import time
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import discord
from PIL import Image, ImageDraw, ImageFont
from discord import app_commands
from discord.ext import commands

from botcore import ui, vc_icons
from botcore.moderation_embeds import build_notice_dm, moderation_emojis_for
from botcore.perms import admin_prefix, admin_slash, dangerous, is_admin, role_problem


JTC_BANNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "jtc" / "voice_interface_banner.png"
JTC_BANNER_FILENAME = "voice_interface_banner.png"


def settings_of(state: dict[str, Any], guild_id: int) -> dict:
    return state["settings"].setdefault(str(guild_id), {})


# ======================================================================= AUTO ROLE
class AutoRoleCog(commands.Cog, name="Auto Role"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def cfg(self, guild_id: int) -> dict:
        cfg = settings_of(self.state, guild_id).setdefault("autorole", {})
        cfg.setdefault("human", [])
        cfg.setdefault("bot", [])
        return cfg

    def feature_on(self, guild: discord.Guild) -> bool:
        cfg = self.cfg(guild.id)
        return bool(cfg["human"] or cfg["bot"])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        ids = self.cfg(guild.id)["bot" if member.bot else "human"]
        me = guild.me
        roles = []
        for role_id in ids:
            role = guild.get_role(role_id)
            if role is not None and me is not None and role < me.top_role and not role.managed and not dangerous(role):
                roles.append(role)
        if roles:
            try:
                await member.add_roles(*roles, reason="Auto role")
            except (discord.Forbidden, discord.HTTPException):
                pass

    def add(self, guild: discord.Guild, actor, role: discord.Role, target: str) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        if dangerous(role):
            return ui.error("Unsafe Role", "Roles with administrator, ban, kick, manage or mention-everyone permissions cannot be given automatically.")
        cfg = self.cfg(guild.id)
        keys = ["human", "bot"] if target == "all" else [target]
        for key in keys:
            if role.id not in cfg[key]:
                cfg[key].append(role.id)
        self.save()
        label = {"human": "every new member", "bot": "every new bot", "all": "every new member and bot"}[target]
        return ui.success("Auto Role Added", f"{role.mention} will now be given to {label}.")

    def remove(self, guild: discord.Guild, role: discord.Role) -> discord.Embed:
        cfg = self.cfg(guild.id)
        found = False
        for key in ("human", "bot"):
            if role.id in cfg[key]:
                cfg[key].remove(role.id)
                found = True
        if not found:
            return ui.warn("Not An Auto Role", f"{role.mention} is not an auto role.")
        self.save()
        return ui.success("Auto Role Removed", f"{role.mention} will no longer be given automatically.")

    def listing(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id)
        humans = " ".join(f"<@&{i}>" for i in cfg["human"]) or "`None`"
        bots = " ".join(f"<@&{i}>" for i in cfg["bot"]) or "`None`"
        return ui.panel("Auto Roles", "Roles given automatically when someone joins.", [("Members", humans), ("Bots", bots)], thumbnail=self.bot.user.display_avatar.url if self.bot.user else None)

    def clear(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id)
        cfg["human"], cfg["bot"] = [], []
        self.save()
        return ui.success("Auto Roles Cleared", "No role will be given automatically anymore.")

    @commands.group(name="autorole", invoke_without_command=True, brief="Give roles automatically on join.",
                    extras={"category": "Auto Role", "usage": ".autorole add|remove|list|clear"})
    @commands.guild_only()
    @admin_prefix
    async def autorole_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing(ctx.guild))

    @autorole_prefix.command(name="add", brief="Add an auto role: .autorole add @role [human|bot|all]")
    @commands.guild_only()
    @admin_prefix
    async def ar_add(self, ctx: commands.Context, role: discord.Role, target: str = "human") -> None:
        target = target.lower()
        if target not in ("human", "bot", "all"):
            await ctx.send(embed=ui.warn("Invalid Target", "Use `human`, `bot` or `all`."))
            return
        await ctx.send(embed=self.add(ctx.guild, ctx.author, role, target))

    @autorole_prefix.command(name="remove", brief="Remove an auto role: .autorole remove @role")
    @commands.guild_only()
    @admin_prefix
    async def ar_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        await ctx.send(embed=self.remove(ctx.guild, role))

    @autorole_prefix.command(name="list", brief="Show the auto roles.")
    @commands.guild_only()
    @admin_prefix
    async def ar_list(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing(ctx.guild))

    @autorole_prefix.command(name="clear", brief="Remove every auto role.")
    @commands.guild_only()
    @admin_prefix
    async def ar_clear(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.clear(ctx.guild))

    autorole = app_commands.Group(name="autorole", description="Automatic roles", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @autorole.command(name="add", description="Give a role to every new member")
    @app_commands.choices(target=[app_commands.Choice(name="Members", value="human"), app_commands.Choice(name="Bots", value="bot"), app_commands.Choice(name="Members and bots", value="all")])
    @admin_slash
    async def s_add(self, interaction: discord.Interaction, role: discord.Role, target: Optional[app_commands.Choice[str]] = None) -> None:
        await ui.respond(interaction, self.add(interaction.guild, interaction.user, role, target.value if target else "human"))

    @autorole.command(name="remove", description="Stop giving a role automatically")
    @admin_slash
    async def s_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await ui.respond(interaction, self.remove(interaction.guild, role))

    @autorole.command(name="list", description="Show the auto roles")
    @admin_slash
    async def s_list(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.listing(interaction.guild))

    @autorole.command(name="clear", description="Remove every auto role")
    @admin_slash
    async def s_clear(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.clear(interaction.guild))


# ======================================================================= WELCOMER
DEFAULT_WELCOME = "Welcome to **{server}**, {user}! You are member **#{count}**."
DEFAULT_LEAVE = "**{name}** has left **{server}**."
DEFAULT_WELCOME_COLOR = "#2ECC71"
CUSTOM_EMOJI_MENTION = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{1,20})(?P<close>>)?"
)
CUSTOM_EMOJI_SHORTCODE = re.compile(r"(?<![<\\A-Za-z0-9]):([A-Za-z0-9_]{2,32}):")
log = logging.getLogger(__name__)


class WelcomerCog(commands.Cog, name="Welcomer"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def cfg(self, guild_id: int) -> dict:
        cfg = settings_of(self.state, guild_id).setdefault("welcome", {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("channel_id", 0)
        cfg.setdefault("leave_channel_id", cfg["channel_id"])
        cfg.setdefault("title", "Welcome")
        cfg.setdefault("message", DEFAULT_WELCOME)
        cfg.setdefault("color", DEFAULT_WELCOME_COLOR)
        cfg.setdefault("image_url", "")
        cfg.setdefault("video_url", "")
        cfg.setdefault("style", "embed")
        cfg.setdefault("font_size", 36)
        cfg.setdefault("title_font_size", 48)
        cfg.setdefault("dm_enabled", False)
        cfg.setdefault("leave_enabled", False)
        cfg.setdefault("leave_message", DEFAULT_LEAVE)
        return cfg

    def feature_on(self, guild: discord.Guild) -> bool:
        cfg = self.cfg(guild.id)
        return bool(cfg["enabled"] and cfg["channel_id"])

    def render(self, text: str, member: discord.Member) -> str:
        rendered = (
            text.replace("{user}", member.mention)
            .replace("{name}", member.display_name)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count))
        )
        emoji_guilds = [member.guild]
        emoji_guilds.extend(
            guild for guild in getattr(getattr(self, "bot", None), "guilds", ())
            if guild.id != member.guild.id
        )
        named_emojis: dict[str, tuple[Any, int]] = {}
        normalized_emojis: dict[str, dict[int, tuple[Any, int]]] = {}
        for emoji_guild in emoji_guilds:
            for emoji in getattr(emoji_guild, "emojis", ()):
                record = (emoji, emoji_guild.id)
                named_emojis.setdefault(emoji.name.casefold(), record)
                normalized_name = re.sub(r"[^a-z0-9]", "", emoji.name.casefold())
                normalized_emojis.setdefault(normalized_name, {}).setdefault(emoji.id, record)

        def lookup_emoji(name: str) -> Any:
            exact = named_emojis.get(name.casefold())
            if exact is not None:
                return exact[0]
            candidates = list(normalized_emojis.get(re.sub(r"[^a-z0-9]", "", name.casefold()), {}).values())
            local_candidates = [record for record in candidates if record[1] == member.guild.id]
            if len(local_candidates) == 1:
                return local_candidates[0][0]
            return candidates[0][0] if len(candidates) == 1 else None

        def emoji_markup(emoji: Any) -> str:
            prefix = "a" if emoji.animated else ""
            return f"<{prefix}:{emoji.name}:{emoji.id}>"

        unresolved: set[str] = set()
        complete_mentions: dict[str, set[str]] = {}
        for match in CUSTOM_EMOJI_MENTION.finditer(rendered):
            if match.group("close") and 17 <= len(match.group("id")) <= 20:
                complete_mentions.setdefault(match.group("name").casefold(), set()).add(match.group(0))

        def resolve_mention(match: re.Match[str]) -> str:
            name = match.group("name")
            emoji_id = match.group("id")
            if match.group("close") and 17 <= len(emoji_id) <= 20:
                return ui.WELCOME_EMOJI_MAP.get(emoji_id, match.group(0))
            known_mentions = complete_mentions.get(name.casefold(), set())
            if len(known_mentions) == 1:
                known_mention = next(iter(known_mentions))
                known_match = CUSTOM_EMOJI_MENTION.fullmatch(known_mention)
                if known_match is not None:
                    return ui.WELCOME_EMOJI_MAP.get(known_match.group("id"), known_mention)
                return known_mention
            if 17 <= len(emoji_id) <= 20:
                return f"{match.group(0)}>"
            emoji = lookup_emoji(name)
            if emoji is not None:
                return emoji_markup(emoji)
            unresolved.add(name)
            return match.group(0)

        rendered = CUSTOM_EMOJI_MENTION.sub(resolve_mention, rendered)

        def resolve_shortcode(match: re.Match[str]) -> str:
            emoji = lookup_emoji(match.group(1))
            if emoji is None:
                unresolved.add(match.group(1))
                return match.group(0)
            return emoji_markup(emoji)

        result = CUSTOM_EMOJI_SHORTCODE.sub(resolve_shortcode, rendered)
        if unresolved:
            log.warning(
                "Welcome custom emoji names were not found in any bot guild: guild_id=%s names=%s",
                member.guild.id,
                ", ".join(sorted(unresolved)),
            )
        return result

    def channel_for(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel = guild.get_channel(self.cfg(guild.id)["channel_id"])
        return channel if isinstance(channel, discord.TextChannel) else None

    def leave_channel_for(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = self.cfg(guild.id)
        channel_id = cfg.get("leave_channel_id") or cfg["channel_id"]
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    def build(self, member: discord.Member, text: str, title: str, kind: str) -> discord.Embed:
        if kind == "success":
            title = self.cfg(member.guild.id).get("title", title)
            title = (
                title.replace("{user}", member.display_name)
                .replace("{name}", member.display_name)
                .replace("{server}", member.guild.name)
                .replace("{count}", str(member.guild.member_count))
            )
        emb = ui.make(kind, title, self.render(text, member) if text else None)
        if kind == "success":
            color = self.cfg(member.guild.id).get("color", DEFAULT_WELCOME_COLOR)
            if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                emb.color = discord.Color(int(color[1:], 16))
        emb.set_thumbnail(url=member.display_avatar.url)
        image_url = self.cfg(member.guild.id).get("image_url", "")
        if kind == "success" and image_url:
            emb.set_image(url=image_url)
        emb.set_footer(text=f"Member #{member.guild.member_count} • {ui.BRAND}")
        return emb

    @staticmethod
    def card_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font_paths = (
            Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        )
        for path in font_paths:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        return ImageFont.load_default(size=size)

    def image_card(self, member: discord.Member, text: str) -> discord.File:
        width, height = 1200, 480
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        cfg = self.cfg(member.guild.id)
        accent = cfg.get("color", DEFAULT_WELCOME_COLOR)
        if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            accent = DEFAULT_WELCOME_COLOR
        accent_rgb = tuple(bytes.fromhex(accent[1:]))
        for y in range(height):
            ratio = y / max(1, height - 1)
            base_color = (
                round(17 + ratio * 13),
                round(25 + ratio * 15),
                round(43 + ratio * 20),
            )
            color = tuple(round(base * 0.35 + accent * 0.65) for base, accent in zip(base_color, accent_rgb))
            draw.line((0, y, width, y), fill=color)
        draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=30, outline=accent_rgb, width=3)
        draw.rounded_rectangle((64, 64, 76, height - 64), radius=6, fill=accent_rgb)
        draw.ellipse((1000, -150, 1310, 160), outline=(46, 61, 84), width=3)
        draw.ellipse((1040, -110, 1270, 120), outline=(40, 54, 76), width=2)

        configured_size = cfg.get("font_size", 36)
        font_size = configured_size if type(configured_size) is int and 18 <= configured_size <= 56 else 36
        configured_title_size = cfg.get("title_font_size", 48)
        title_font_size = (
            configured_title_size
            if type(configured_title_size) is int and 24 <= configured_title_size <= 72
            else 48
        )
        title_font = self.card_font(title_font_size, bold=True)
        body_font = self.card_font(font_size)
        label_font = self.card_font(18, bold=True)
        title = cfg.get("title", "Welcome")
        title = self.render(str(title), member)
        title = re.sub(r"<@!?\d+>", f"@{member.display_name}", title)
        title = re.sub(r"[*_~`]", "", title).strip() or "Welcome"
        message = self.render(text, member)
        message = re.sub(r"<@!?\d+>", f"@{member.display_name}", message)
        message = re.sub(r"<#\d+>", "#channel", message)
        message = re.sub(r"<@&\d+>", "@role", message)
        message = re.sub(r"<a?:([A-Za-z0-9_]{2,32}):\d{17,20}>", r":\1:", message)
        message = re.sub(r"[*_~`]", "", message).replace("\n", " ")

        def fit_line(value: str, font: ImageFont.ImageFont, max_width: int) -> str:
            original = value
            while value and draw.textlength(value, font=font) > max_width:
                value = value[:-1]
            return value.rstrip() + ("…" if len(value) < len(original) else "")

        title_line = title
        while title_line and draw.textlength(title_line, font=title_font) > 960:
            title_line = title_line[:-1]
        if title_line != title:
            title_line = title_line.rstrip() + "…"

        display_line = f"{member.display_name[:48]}  •  {member.guild.name[:48]}"
        if draw.textlength(display_line, font=body_font) > 960:
            display_line = fit_line(display_line, body_font, 940)

        message_lines: list[str] = []
        current_line = ""
        truncated = False
        for word in message.split():
            candidate = f"{current_line} {word}".strip()
            if current_line and draw.textlength(candidate, font=body_font) > 960:
                message_lines.append(current_line)
                current_line = word
                if len(message_lines) == 2:
                    truncated = True
                    break
            else:
                current_line = candidate
        if len(message_lines) < 2 and current_line:
            message_lines.append(current_line)
        if truncated:
            message_lines[-1] = fit_line(message_lines[-1] + "…", body_font, 920)
        message_lines = [fit_line(line, body_font, 960) for line in message_lines]

        draw.text((112, 90), "K.R.Y.O  /  COMMUNITY", font=label_font, fill=accent_rgb)
        draw.text((112, 135), title_line, font=title_font, fill=(245, 248, 255))
        draw.text((112, 218), display_line, font=body_font, fill=(190, 202, 220))
        draw.multiline_text((112, 278), "\n".join(message_lines), font=body_font, spacing=8, fill=(226, 232, 242))
        draw.text((112, 414), f"MEMBER  #{member.guild.member_count}", font=label_font, fill=accent_rgb)

        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return discord.File(output, filename="welcome-card.png")

    def welcome_payload(self, member: discord.Member, text: str) -> dict[str, Any]:
        cfg = self.cfg(member.guild.id)
        style = cfg.get("style", "embed")
        rendered = self.render(text, member)
        video_url = cfg.get("video_url", "")
        allowed_mentions = discord.AllowedMentions(
            users=True,
            roles=False,
            everyone=False,
            replied_user=False,
        )
        if style == "plain":
            content = rendered
            if video_url:
                notice = f"\n\nWatch the welcome video: {video_url}"
                content = content[:2000 - len(notice)] + notice
            return {"content": content[:2000], "allowed_mentions": allowed_mentions}

        embed = self.build(member, text, "Welcome", "success")
        payload: dict[str, Any] = {"embed": embed, "allowed_mentions": allowed_mentions}
        if video_url:
            payload["content"] = f"Watch the welcome video: {video_url}"
        if style == "image_card":
            card = self.image_card(member, text)
            embed.set_image(url="attachment://welcome-card.png")
            payload["file"] = card
            image_url = cfg.get("image_url", "")
            if image_url:
                embed.set_thumbnail(url=image_url)
        return payload

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        channel = self.channel_for(member.guild)
        if not cfg["enabled"] or channel is None:
            return
        try:
            await channel.send(**self.welcome_payload(member, cfg["message"]))
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("Could not send welcome message guild=%s member=%s: %s", member.guild.id, member.id, error)
            return
        if cfg.get("dm_enabled"):
            try:
                await member.send(**self.welcome_payload(member, cfg["message"]))
            except (discord.Forbidden, discord.HTTPException) as error:
                log.info("Could not DM welcome message guild=%s member=%s: %s", member.guild.id, member.id, error)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        channel = self.leave_channel_for(member.guild)
        if not cfg["leave_enabled"] or channel is None:
            return
        try:
            await channel.send(embed=self.build(member, cfg["leave_message"], "Goodbye", "warn"))
        except (discord.Forbidden, discord.HTTPException):
            pass

    def set_channel(self, guild: discord.Guild, channel: discord.TextChannel) -> discord.Embed:
        cfg = self.cfg(guild.id)
        cfg["channel_id"], cfg["enabled"] = channel.id, True
        self.save()
        return ui.success("Welcomer Enabled", f"Welcome messages will be sent to {channel.mention}.")

    def set_message(self, guild: discord.Guild, text: str, leave: bool = False) -> discord.Embed:
        cfg = self.cfg(guild.id)
        cfg["leave_message" if leave else "message"] = text[:1000]
        self.save()
        return ui.success("Message Saved", "The new message was saved.\nPlaceholders: `{user}` `{name}` `{server}` `{count}`")

    def toggle(self, guild: discord.Guild, on: bool, leave: bool = False) -> discord.Embed:
        cfg = self.cfg(guild.id)
        if on and not cfg["channel_id"]:
            return ui.warn("Channel Missing", "Choose a channel first with `.welcome channel #channel`.")
        cfg["leave_enabled" if leave else "enabled"] = on
        self.save()
        name = "Leave messages" if leave else "Welcome messages"
        return ui.success(f"{name} {'Enabled' if on else 'Disabled'}", f"{name} are now **{'ON' if on else 'OFF'}**.")

    def status_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id)
        channel = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "`Not set`"
        rows = [
            ("Welcome", f"`{'ON' if cfg['enabled'] else 'OFF'}`"),
            ("Leave", f"`{'ON' if cfg['leave_enabled'] else 'OFF'}`"),
            ("Channel", channel),
        ]
        emb = ui.panel("Welcomer", "Greets new members automatically.", rows, notes=["Placeholders: {user} {name} {server} {count}"])
        emb.add_field(name="Welcome message", value=trim(cfg["message"]), inline=False)
        emb.add_field(name="Leave message", value=trim(cfg["leave_message"]), inline=False)
        return emb

    async def test(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        channel = self.channel_for(guild)
        if channel is None:
            return ui.warn("Channel Missing", "Choose a channel first with `.welcome channel #channel`.")
        try:
            await channel.send(**self.welcome_payload(member, self.cfg(guild.id)["message"]))
        except (discord.Forbidden, discord.HTTPException):
            return ui.error("Missing Permissions", f"I cannot send messages in {channel.mention}.")
        return ui.success("Test Sent", f"A test welcome message was sent to {channel.mention}.")

    @commands.group(name="welcome", invoke_without_command=True, brief="Welcome and leave messages.",
                    extras={"category": "Welcomer", "usage": ".welcome channel|message|on|off|leave|test"})
    @commands.guild_only()
    @admin_prefix
    async def welcome_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.status_embed(ctx.guild))

    @welcome_prefix.command(name="channel", brief="Set the channel: .welcome channel #channel")
    @commands.guild_only()
    @admin_prefix
    async def w_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        await ctx.send(embed=self.set_channel(ctx.guild, channel))

    @welcome_prefix.command(name="message", brief="Set the welcome text: .welcome message <text>")
    @commands.guild_only()
    @admin_prefix
    async def w_message(self, ctx: commands.Context, *, text: str) -> None:
        await ctx.send(embed=self.set_message(ctx.guild, text))

    @welcome_prefix.command(name="leavemessage", brief="Set the leave text: .welcome leavemessage <text>")
    @commands.guild_only()
    @admin_prefix
    async def w_leave_message(self, ctx: commands.Context, *, text: str) -> None:
        await ctx.send(embed=self.set_message(ctx.guild, text, leave=True))

    @welcome_prefix.command(name="on", brief="Turn welcome messages on.")
    @commands.guild_only()
    @admin_prefix
    async def w_on(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.toggle(ctx.guild, True))

    @welcome_prefix.command(name="off", brief="Turn welcome messages off.")
    @commands.guild_only()
    @admin_prefix
    async def w_off(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.toggle(ctx.guild, False))

    @welcome_prefix.command(name="leave", brief="Leave messages: .welcome leave on|off")
    @commands.guild_only()
    @admin_prefix
    async def w_leave(self, ctx: commands.Context, state: str) -> None:
        if state.lower() not in ("on", "off"):
            await ctx.send(embed=ui.warn("Usage", "`.welcome leave on` or `.welcome leave off`"))
            return
        await ctx.send(embed=self.toggle(ctx.guild, state.lower() == "on", leave=True))

    @welcome_prefix.command(name="test", brief="Send a test welcome message.")
    @commands.guild_only()
    @admin_prefix
    async def w_test(self, ctx: commands.Context) -> None:
        await ctx.send(embed=await self.test(ctx.guild, ctx.author))

    welcome = app_commands.Group(name="welcome", description="Welcome messages", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @welcome.command(name="channel", description="Set the welcome channel and turn it on")
    @admin_slash
    async def s_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await ui.respond(interaction, self.set_channel(interaction.guild, channel))

    @welcome.command(name="message", description="Set the welcome text ({user} {name} {server} {count})")
    @admin_slash
    async def s_message(self, interaction: discord.Interaction, text: str) -> None:
        await ui.respond(interaction, self.set_message(interaction.guild, text))

    @welcome.command(name="toggle", description="Turn welcome messages on or off")
    @admin_slash
    async def s_toggle(self, interaction: discord.Interaction, state: Literal["on", "off"]) -> None:
        await ui.respond(interaction, self.toggle(interaction.guild, state == "on"))

    @welcome.command(name="leave", description="Turn leave messages on or off")
    @admin_slash
    async def s_leave(self, interaction: discord.Interaction, state: Literal["on", "off"]) -> None:
        await ui.respond(interaction, self.toggle(interaction.guild, state == "on", leave=True))

    @welcome.command(name="test", description="Send a test welcome message")
    @admin_slash
    async def s_test(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, await self.test(interaction.guild, interaction.user), ephemeral=True)

    @welcome.command(name="status", description="Show the welcomer settings")
    @admin_slash
    async def s_status(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.status_embed(interaction.guild))


def trim(text: str, limit: int = 900) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ======================================================================= SELF ROLES
class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kryo:selfrole:(?P<role_id>[0-9]+)"):
    """Persistent button: click to get the role, click again to remove it."""

    def __init__(self, role_id: int, label: str = "Role") -> None:
        super().__init__(
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, emoji=ui.SPIDER, custom_id=f"kryo:selfrole:{role_id}")
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["role_id"]), item.label or "Role")

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        cog_getter = getattr(interaction.client, "get_cog", None)
        cog = cog_getter("Self Roles") if callable(cog_getter) else None
        if guild is None or cog is None or self.role_id not in cog.roles_of(guild.id):
            await ui.respond(interaction, ui.error("Role Unavailable", "This role is no longer on the self-role panel."), ephemeral=True)
            return
        role = guild.get_role(self.role_id) if guild else None
        if role is None or not isinstance(member, discord.Member):
            await ui.respond(interaction, ui.error("Role Missing", "This role no longer exists."), ephemeral=True)
            return
        if dangerous(role) or role.managed or guild.me is None or role >= guild.me.top_role:
            await ui.respond(interaction, ui.error("Role Unavailable", "I cannot hand out this role anymore. Please tell the staff."), ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self role")
                await ui.respond(interaction, ui.success("Role Removed", f"{role.mention} was removed from you."), ephemeral=True)
            else:
                await member.add_roles(role, reason="Self role")
                await ui.respond(interaction, ui.success("Role Added", f"You now have {role.mention}."), ephemeral=True)
        except discord.Forbidden:
            await ui.respond(interaction, ui.error("Missing Permissions", "I could not change your roles."), ephemeral=True)
        except discord.HTTPException:
            await ui.respond(interaction, ui.error("Role Update Failed", "Discord could not update your roles. Try again."), ephemeral=True)


class SelfRoleSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"kryo:selfrole-select"):
    """Persistent dropdown; each selection toggles one configured role."""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(discord.ui.Select(
            placeholder="Choose a role to add or remove",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="kryo:selfrole-select",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        return cls(item.options)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        values = interaction.data.get("values", [])
        if guild is None or not isinstance(member, discord.Member) or len(values) != 1:
            await ui.respond(interaction, ui.error("Role Unavailable", "Choose a role from this server's panel."), ephemeral=True)
            return
        cog_getter = getattr(interaction.client, "get_cog", None)
        cog = cog_getter("Self Roles") if callable(cog_getter) else None
        if cog is None:
            await ui.respond(interaction, ui.error("Role Unavailable", "Self roles are not available right now."), ephemeral=True)
            return
        try:
            role_id = int(values[0])
        except (TypeError, ValueError):
            await ui.respond(interaction, ui.error("Role Unavailable", "That role is no longer available."), ephemeral=True)
            return
        if role_id not in cog.roles_of(guild.id):
            await ui.respond(interaction, ui.error("Role Unavailable", "That role is no longer on the self-role panel."), ephemeral=True)
            return
        role = guild.get_role(role_id)
        if role is None or dangerous(role) or role.managed or guild.me is None or role >= guild.me.top_role:
            await ui.respond(interaction, ui.error("Role Unavailable", "I cannot hand out this role anymore. Please tell the staff."), ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self role")
                await ui.respond(interaction, ui.success("Role Removed", f"{role.mention} was removed from you."), ephemeral=True)
            else:
                await member.add_roles(role, reason="Self role")
                await ui.respond(interaction, ui.success("Role Added", f"You now have {role.mention}."), ephemeral=True)
        except discord.Forbidden:
            await ui.respond(interaction, ui.error("Missing Permissions", "I could not change your roles."), ephemeral=True)
        except discord.HTTPException:
            await ui.respond(interaction, ui.error("Role Update Failed", "Discord could not update your roles. Try again."), ephemeral=True)


class SelfRolesCog(commands.Cog, name="Self Roles"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, SelfRoleSelect)

    def roles_of(self, guild_id: int) -> list[int]:
        return settings_of(self.state, guild_id).setdefault("selfroles", [])

    def add(self, guild: discord.Guild, actor, role: discord.Role) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        if dangerous(role):
            return ui.error("Unsafe Role", "Roles with administrator, ban, kick, manage or mention-everyone permissions cannot be self roles.")
        roles = self.roles_of(guild.id)
        if role.id in roles:
            return ui.warn("Already Added", f"{role.mention} is already a self role.")
        if len(roles) >= 25:
            return ui.warn("Limit Reached", "A panel can hold up to 25 roles.")
        roles.append(role.id)
        self.save()
        return ui.success("Self Role Added", f"{role.mention} was added. Publish the panel with `.selfrole panel`.")

    def remove(self, guild: discord.Guild, role: discord.Role) -> discord.Embed:
        roles = self.roles_of(guild.id)
        if role.id not in roles:
            return ui.warn("Not A Self Role", f"{role.mention} is not a self role.")
        roles.remove(role.id)
        self.save()
        return ui.success("Self Role Removed", f"{role.mention} was removed. Publish the panel again to update its dropdown.")

    def listing(self, guild: discord.Guild) -> discord.Embed:
        roles = self.roles_of(guild.id)
        text = "\n".join(f"{ui.SPIDER} <@&{role_id}>" for role_id in roles) or "No self roles yet. Add one with `.selfrole add @role`."
        return ui.info("Self Roles", text)

    async def publish_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> discord.Message:
        role_ids = self.roles_of(guild.id)
        if not role_ids or len(role_ids) > 25:
            raise ValueError("Choose between 1 and 25 self-assignable roles before publishing.")
        roles = [guild.get_role(role_id) for role_id in role_ids]
        if any(
            role is None
            or dangerous(role)
            or role.managed
            or guild.me is None
            or role >= guild.me.top_role
            for role in roles
        ):
            raise ValueError("A self-role is missing, unsafe, managed, or above the bot's role. Update the selection and try again.")
        roles = [role for role in roles if role is not None]
        config = settings_of(self.state, guild.id).get("selfrole_panel", {})
        config = config if isinstance(config, dict) else {}
        panel_title = title if title is not None else config.get("title", "Choose your roles")
        panel_description = description if description is not None else config.get(
            "description",
            "Choose any roles that fit you. Select a role again to remove it.",
        )
        configured_options = config.get("options", [])
        labels_by_role: dict[int, str] = {}
        if isinstance(configured_options, list):
            for option in configured_options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    continue
                try:
                    role_id = int(option.get("role_id", 0))
                except (TypeError, ValueError):
                    continue
                if role_id in role_ids and option["label"].strip():
                    labels_by_role[role_id] = option["label"].strip()[:100]
        options = [
            discord.SelectOption(label=labels_by_role.get(role.id, role.name)[:100], value=str(role.id))
            for role in roles
        ]
        view = discord.ui.View(timeout=None)
        view.add_item(SelfRoleSelect(options))
        embed = ui.panel(
            panel_title,
            panel_description,
            [(labels_by_role.get(role.id, role.name), role.mention) for role in roles],
        )
        guild_settings = settings_of(self.state, guild.id)
        panel_config = guild_settings.get("selfrole_panel")
        if not isinstance(panel_config, dict):
            panel_config = {}
            guild_settings["selfrole_panel"] = panel_config
        existing_message = None
        try:
            old_channel_id = int(panel_config.get("message_channel_id", 0) or 0)
            old_message_id = int(panel_config.get("message_id", 0) or 0)
        except (TypeError, ValueError):
            old_channel_id = old_message_id = 0
        if old_channel_id == channel.id and old_message_id:
            try:
                existing_message = await channel.fetch_message(old_message_id)
            except discord.NotFound:
                existing_message = None
        bot_user = self.bot.user
        if existing_message is not None and bot_user is not None and existing_message.author.id == bot_user.id:
            message = await existing_message.edit(embed=embed, view=view)
        else:
            message = await channel.send(embed=embed, view=view)
        panel_config["message_channel_id"] = channel.id
        panel_config["message_id"] = message.id
        self.save()
        return message

    async def post_panel(self, guild: discord.Guild, channel: discord.TextChannel) -> discord.Embed:
        try:
            message = await self.publish_panel(guild, channel)
        except ValueError as error:
            return ui.warn("Self-Role Panel Unavailable", str(error))
        except (discord.Forbidden, discord.HTTPException):
            return ui.error("Missing Permissions", f"I cannot send or update a panel in {channel.mention}.")
        return ui.success("Panel Posted", f"The self-role panel is ready in {channel.mention}: [open message]({message.jump_url}).")

    def clear(self, guild: discord.Guild) -> discord.Embed:
        self.roles_of(guild.id).clear()
        self.save()
        return ui.success("Self Roles Cleared", "The self role list is empty now.")

    @commands.group(name="selfrole", invoke_without_command=True, brief="Dropdown roles members can pick themselves.",
                    extras={"category": "Self Roles", "usage": ".selfrole add|remove|list|panel|clear"})
    @commands.guild_only()
    @admin_prefix
    async def selfrole_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing(ctx.guild))

    @selfrole_prefix.command(name="add", brief="Add a role: .selfrole add @role")
    @commands.guild_only()
    @admin_prefix
    async def sr_add(self, ctx: commands.Context, role: discord.Role) -> None:
        await ctx.send(embed=self.add(ctx.guild, ctx.author, role))

    @selfrole_prefix.command(name="remove", brief="Remove a role: .selfrole remove @role")
    @commands.guild_only()
    @admin_prefix
    async def sr_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        await ctx.send(embed=self.remove(ctx.guild, role))

    @selfrole_prefix.command(name="list", brief="Show the self roles.")
    @commands.guild_only()
    @admin_prefix
    async def sr_list(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.listing(ctx.guild))

    @selfrole_prefix.command(name="panel", brief="Post the dropdown panel: .selfrole panel [#channel]")
    @commands.guild_only()
    @admin_prefix
    async def sr_panel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.send(embed=await self.post_panel(ctx.guild, channel or ctx.channel))

    @selfrole_prefix.command(name="clear", brief="Empty the self role list.")
    @commands.guild_only()
    @admin_prefix
    async def sr_clear(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.clear(ctx.guild))

    selfrole = app_commands.Group(name="selfrole", description="Self roles", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @selfrole.command(name="add", description="Add a role to the self role list")
    @admin_slash
    async def s_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await ui.respond(interaction, self.add(interaction.guild, interaction.user, role))

    @selfrole.command(name="remove", description="Remove a role from the self role list")
    @admin_slash
    async def s_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await ui.respond(interaction, self.remove(interaction.guild, role))

    @selfrole.command(name="list", description="Show the self roles")
    @admin_slash
    async def s_list(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.listing(interaction.guild))

    @selfrole.command(name="panel", description="Post the self-role dropdown panel")
    @admin_slash
    async def s_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await ui.respond(interaction, await self.post_panel(interaction.guild, channel or interaction.channel), ephemeral=True)


# ======================================================================= JTC INTERFACE PANEL
PANEL_BUTTON_ROWS = (
    (
        ("Lock", "lock", "lock", discord.ButtonStyle.secondary),
        ("Unlock", "unlock", "unlock", discord.ButtonStyle.secondary),
        ("Hide", "hide", "hide", discord.ButtonStyle.secondary),
        ("Unhide", "unhide", "unhide", discord.ButtonStyle.secondary),
    ),
    (
        ("Permit", "permit", "permit", discord.ButtonStyle.success),
        ("Permit All", "permitall", "permit_all", discord.ButtonStyle.success),
        ("Invite", "invite", "invite", discord.ButtonStyle.primary),
    ),
    (
        ("Vc Ban", "vcban", "vcban", discord.ButtonStyle.danger),
        ("VcUnban", "vcunban", "vcunban", discord.ButtonStyle.secondary),
        ("Mute", "mute", "mute", discord.ButtonStyle.secondary),
        ("Unmute", "unmute", "unmute", discord.ButtonStyle.secondary),
    ),
    (
        ("Transfer", "transfer", "transfer", discord.ButtonStyle.primary),
        ("Claim", "claim", "claim", discord.ButtonStyle.primary),
    ),
    (
        ("Rename", "rename", "rename", discord.ButtonStyle.secondary),
        ("Limit", "limit", "limit", discord.ButtonStyle.secondary),
        ("Bitrate", "bitrate", "bitrate", discord.ButtonStyle.secondary),
    ),
)
USER_ACTIONS = {
    "transfer": ("Transfer Ownership", "Choose the member (inside your channel) who becomes the new owner."),
    "invite": ("Invite Member", "Choose a member to invite. They get access and a private invite."),
    "permit": ("Permit Member", "Choose a member who may join even when the channel is locked."),
    "vcban": ("Vc Ban Member", "Choose a member to ban from your channel."),
    "vcunban": ("Vc Unban Member", "Choose a member to unban from your channel."),
    "mute": ("Mute Member", "Choose a member who should not be able to speak in your channel."),
    "unmute": ("Unmute Member", "Choose a member who may speak again."),
}
MODAL_ACTIONS = {
    "limit": ("Set User Limit", "Limit (0 = unlimited, max 99)", "5", 2),
    "rename": ("Rename Channel", "New channel name", "My channel", 100),
    "bitrate": ("Set Bitrate", "Bitrate in kbps", "64", 3),
}


class JtcPanel(discord.ui.LayoutView):
    """Persistent Join To Create controls arranged to match the K.R.Y.O voice panel."""

    def __init__(self, cog: "JoinToCreateCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(
            discord.ui.Container(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        f"attachment://{JTC_BANNER_FILENAME}",
                        description="KRYO Voice Interface",
                    )
                ),
                discord.ui.TextDisplay(
                    "Control your own temporary voice channel with the buttons below."
                ),
                accent_colour=0x8054F6,
            )
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"**{vc_icons.text('sec_access')} Access**"),
                self._button_row(0),
                self._button_row(1),
                discord.ui.TextDisplay(f"---\n**{vc_icons.text('sec_members')} Members**"),
                self._button_row(2),
                self._button_row(3),
                discord.ui.TextDisplay(f"---\n**{vc_icons.text('sec_channel')} Channel**"),
                self._button_row(4),
                discord.ui.TextDisplay(
                    "---\n"
                    "-# Join the Join To Create channel to get your own voice channel\n"
                    "-# Only the owner of a channel can use these buttons\n"
                    "-# KRYO • Security & Music"
                ),
                accent_colour=0x8054F6,
            )
        )

    def _button_row(self, row_index: int) -> discord.ui.ActionRow:
        buttons = []
        for label, key, icon_name, style in PANEL_BUTTON_ROWS[row_index]:
            button = discord.ui.Button(
                label=label,
                style=style,
                emoji=vc_icons.icon(icon_name),
                custom_id=f"kryo:jtc:{key}",
            )
            button.callback = self._callback(key)
            buttons.append(button)
        return discord.ui.ActionRow(*buttons)

    def _callback(self, key: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.panel_action(interaction, key)

        return callback


class UserPickView(discord.ui.View):
    def __init__(self, cog: "JoinToCreateCog", key: str, channel_id: int, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.key = key
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.message: Optional[discord.Message] = None
        self.select = discord.ui.UserSelect(placeholder="Choose a member", min_values=1, max_values=1)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await ui.respond(interaction, ui.error("Not Your Menu", "Only the channel owner can use this."), ephemeral=True)
        return False

    async def picked(self, interaction: discord.Interaction) -> None:
        await self.cog.user_action(interaction, self.key, self.channel_id, self.select.values[0])
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.NotFound:
            return
        except discord.HTTPException as error:
            print(f"[jtc] could not disable expired member picker: {type(error).__name__}: {error}")


class PanelModal(discord.ui.Modal):
    def __init__(self, cog: "JoinToCreateCog", key: str, channel_id: int) -> None:
        title, label, placeholder, length = MODAL_ACTIONS[key]
        super().__init__(title=title)
        self.cog = cog
        self.key = key
        self.channel_id = channel_id
        self.field = discord.ui.TextInput(label=label, placeholder=placeholder, max_length=length, required=True)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.modal_action(interaction, self.key, self.channel_id, str(self.field.value).strip())


# ======================================================================= JOIN TO CREATE
class JoinToCreateCog(commands.Cog, name="Join To Create"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.last_create: dict[int, float] = {}

    async def cog_load(self) -> None:
        await vc_icons.setup_icons(self.bot)
        # keeps the interface buttons working after a restart
        self.bot.add_view(JtcPanel(self))

    def cfg(self, guild_id: int) -> dict:
        cfg = settings_of(self.state, guild_id).setdefault("jtc", {})
        cfg.setdefault("hub_id", 0)
        cfg.setdefault("temp", {})
        return cfg

    def feature_on(self, guild: discord.Guild) -> bool:
        return bool(self.cfg(guild.id)["hub_id"])

    # ---- automatic part
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            cfg = self.cfg(guild.id)
            for channel_id in list(cfg["temp"]):
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    cfg["temp"].pop(channel_id, None)
                elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                    try:
                        await channel.delete(reason="Join To Create cleanup")
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                    cfg["temp"].pop(channel_id, None)
        self.save()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        guild = member.guild
        cfg = self.cfg(guild.id)
        if not cfg["hub_id"] or before.channel == after.channel:
            return

        if after.channel is not None and after.channel.id == cfg["hub_id"] and not member.bot:
            now = time.monotonic()
            if now - self.last_create.get(member.id, 0) < 5:
                return
            self.last_create[member.id] = now
            try:
                new = await guild.create_voice_channel(f"{member.display_name}'s VC"[:100], category=after.channel.category, reason="Join To Create")
                await new.set_permissions(
                    member,
                    view_channel=True,
                    connect=True,
                    speak=True,
                    send_messages=True,
                    read_message_history=True,
                    reason="Join To Create owner",
                )
                if guild.me is not None:
                    await new.set_permissions(
                        guild.me,
                        view_channel=True,
                        send_messages=True,
                        embed_links=True,
                        attach_files=True,
                        read_message_history=True,
                        reason="Join To Create control panel",
                    )
                await member.move_to(new, reason="Join To Create")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(
                    f"[jtc] could not configure temporary voice channel "
                    f"guild={guild.id} member={member.id}: {type(error).__name__}: {error}"
                )
                return
            cfg["temp"][str(new.id)] = member.id
            self.save()
            try:
                await new.send(file=self.panel_file(), view=JtcPanel(self))
                print(f"[jtc] posted control panel guild={guild.id} channel={new.id}")
            except discord.HTTPException as error:
                print(
                    f"[jtc] could not post panel in temporary voice channel "
                    f"guild={guild.id} channel={new.id}: {type(error).__name__}: {error}"
                )

        if before.channel is not None and str(before.channel.id) in cfg["temp"] and not before.channel.members:
            try:
                await before.channel.delete(reason="Join To Create: channel is empty")
            except (discord.Forbidden, discord.HTTPException):
                pass
            cfg["temp"].pop(str(before.channel.id), None)
            self.save()

    # ---- setup
    async def setup_hub(self, guild: discord.Guild, channel: Optional[discord.VoiceChannel]) -> discord.Embed:
        me = guild.me
        if me is None or not (me.guild_permissions.administrator or (me.guild_permissions.manage_channels and me.guild_permissions.move_members)):
            return ui.error("Missing Permissions", "I need **Manage Channels** and **Move Members** to run Join To Create.")
        interface = None
        try:
            if channel is None:
                category = await guild.create_category("Join To Create", reason="Join To Create setup")
                channel = await category.create_voice_channel("Join To Create", reason="Join To Create setup")
                interface = await category.create_text_channel(
                    "interface",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False),
                        me: discord.PermissionOverwrite(
                            send_messages=True,
                            embed_links=True,
                            attach_files=True,
                            view_channel=True,
                        ),
                    },
                    reason="Join To Create setup",
                )
        except (discord.Forbidden, discord.HTTPException):
            return ui.error("Setup Failed", "I could not create the channels. Check my permissions.")
        cfg = self.cfg(guild.id)
        cfg["hub_id"] = channel.id
        text = f"Members who join {channel.mention} get their own voice channel.\nIt is deleted automatically when it becomes empty."
        if interface is not None:
            cfg["interface_id"] = interface.id
            try:
                await interface.send(file=self.panel_file(), view=JtcPanel(self))
                text += f"\nThe control panel was posted in {interface.mention}."
            except (discord.Forbidden, discord.HTTPException):
                text += "\nI could not post the control panel. Run `.jtc panel` in a text channel."
        else:
            text += "\nPost the control panel with `.jtc panel`."
        self.save()
        return ui.success("Join To Create Ready", text)

    def disable(self, guild: discord.Guild) -> discord.Embed:
        self.cfg(guild.id)["hub_id"] = 0
        self.save()
        return ui.warn("Join To Create Disabled", "The hub channel was kept, but it no longer creates channels.")

    # ---- owner controls
    def temp_channel(self, member: discord.Member, claim: bool = False) -> tuple[Optional[discord.VoiceChannel], Optional[discord.Embed]]:
        voice = member.voice
        if voice is None or voice.channel is None:
            return None, ui.warn("Not In Voice", "Join your temporary voice channel first.")
        channel = voice.channel
        temp = self.cfg(member.guild.id)["temp"]
        owner_id = temp.get(str(channel.id))
        if owner_id is None:
            return None, ui.warn("Not A Temporary Channel", "This command only works inside a Join To Create channel.")
        if not claim and owner_id != member.id:
            return None, ui.error("Not The Owner", f"Only <@{owner_id}> can manage this channel.")
        return channel, None

    async def control(self, member: discord.Member, title: str, text: str, **changes) -> discord.Embed:
        channel, problem = self.temp_channel(member)
        if problem:
            return problem
        try:
            await channel.edit(reason=f"Join To Create by {member}", **changes)
        except discord.Forbidden:
            return ui.error("Missing Permissions", "I could not edit that channel.")
        return ui.success(title, text.format(channel=channel.mention))

    async def set_lock(self, member: discord.Member, locked: bool) -> discord.Embed:
        channel, problem = self.temp_channel(member)
        if problem:
            return problem
        try:
            await channel.set_permissions(member.guild.default_role, connect=False if locked else None, reason="Join To Create lock")
        except discord.Forbidden:
            return ui.error("Missing Permissions", "I could not edit that channel.")
        return ui.success("Channel Locked" if locked else "Channel Unlocked", f"{channel.mention} is now {'locked' if locked else 'open'}.")

    async def claim(self, member: discord.Member) -> discord.Embed:
        channel, problem = self.temp_channel(member, claim=True)
        if problem:
            return problem
        temp = self.cfg(member.guild.id)["temp"]
        owner_id = temp[str(channel.id)]
        if owner_id == member.id:
            return ui.warn("Already Owner", "You already own this channel.")
        if any(m.id == owner_id for m in channel.members):
            return ui.error("Owner Is Present", "The owner is still in the channel.")
        try:
            await channel.set_permissions(member, view_channel=True, connect=True, speak=True)
        except discord.Forbidden:
            return ui.error("Missing Permissions", "I could not give you ownership permissions.")
        except discord.HTTPException as error:
            print(f"[jtc] claim failed for channel {channel.id}: {type(error).__name__}: {error}")
            return ui.error("Claim Failed", "Discord could not update the channel permissions. Please try again.")
        temp[str(channel.id)] = member.id
        self.save()
        return ui.success("Channel Claimed", f"You are now the owner of {channel.mention}.")

    @commands.group(name="jtc", invoke_without_command=True, brief="Join To Create temporary voice channels.",
                    extras={"category": "Join To Create", "usage": ".jtc setup|disable|name|limit|lock|unlock|claim", "public": True})
    @commands.guild_only()
    async def jtc_prefix(self, ctx: commands.Context) -> None:
        state = "ON" if self.feature_on(ctx.guild) else "OFF"
        await ctx.send(embed=ui.info("Join To Create", f"Status: **{state}**\nAdmins: `.jtc setup` / `.jtc disable`\nChannel owners: `.jtc name`, `.jtc limit`, `.jtc lock`, `.jtc unlock`, `.jtc claim`"))

    @jtc_prefix.command(name="setup", brief="Create the hub channel (admin): .jtc setup [#voice]")
    @commands.guild_only()
    @admin_prefix
    async def jtc_setup(self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None) -> None:
        await ctx.send(embed=await self.setup_hub(ctx.guild, channel))

    @jtc_prefix.command(name="disable", brief="Turn Join To Create off (admin).")
    @commands.guild_only()
    @admin_prefix
    async def jtc_disable(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.disable(ctx.guild))

    @jtc_prefix.command(name="name", brief="Rename your channel: .jtc name <name>")
    @commands.guild_only()
    async def jtc_name(self, ctx: commands.Context, *, name: str) -> None:
        await ctx.send(embed=await self.control(ctx.author, "Channel Renamed", "{channel} was renamed.", name=name[:100]))

    @jtc_prefix.command(name="limit", brief="Set the user limit (0 = none): .jtc limit 5")
    @commands.guild_only()
    async def jtc_limit(self, ctx: commands.Context, limit: int) -> None:
        limit = max(0, min(limit, 99))
        await ctx.send(embed=await self.control(ctx.author, "Limit Updated", f"{{channel}} now allows **{limit or 'unlimited'}** members.", user_limit=limit))

    @jtc_prefix.command(name="lock", brief="Lock your channel.")
    @commands.guild_only()
    async def jtc_lock(self, ctx: commands.Context) -> None:
        await ctx.send(embed=await self.set_lock(ctx.author, True))

    @jtc_prefix.command(name="unlock", brief="Unlock your channel.")
    @commands.guild_only()
    async def jtc_unlock(self, ctx: commands.Context) -> None:
        await ctx.send(embed=await self.set_lock(ctx.author, False))

    @jtc_prefix.command(name="claim", brief="Take over a channel whose owner left.")
    @commands.guild_only()
    async def jtc_claim(self, ctx: commands.Context) -> None:
        await ctx.send(embed=await self.claim(ctx.author))

    jtc = app_commands.Group(name="jtc", description="Join To Create", guild_only=True)

    @jtc.command(name="setup", description="Create the Join To Create hub (admin)")
    @app_commands.default_permissions(administrator=True)
    @admin_slash
    async def s_setup(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None) -> None:
        await interaction.response.defer()
        await ui.respond(interaction, await self.setup_hub(interaction.guild, channel))

    @jtc.command(name="disable", description="Turn Join To Create off (admin)")
    @app_commands.default_permissions(administrator=True)
    @admin_slash
    async def s_disable(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.disable(interaction.guild))

    @jtc.command(name="name", description="Rename your temporary channel")
    async def s_name(self, interaction: discord.Interaction, name: str) -> None:
        await ui.respond(interaction, await self.control(interaction.user, "Channel Renamed", "{channel} was renamed.", name=name[:100]), ephemeral=True)

    @jtc.command(name="limit", description="Set the user limit of your temporary channel")
    async def s_limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]) -> None:
        await ui.respond(interaction, await self.control(interaction.user, "Limit Updated", f"{{channel}} now allows **{limit or 'unlimited'}** members.", user_limit=limit), ephemeral=True)

    @jtc.command(name="lock", description="Lock your temporary channel")
    async def s_lock(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, await self.set_lock(interaction.user, True), ephemeral=True)

    @jtc.command(name="unlock", description="Unlock your temporary channel")
    async def s_unlock(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, await self.set_lock(interaction.user, False), ephemeral=True)

    @jtc.command(name="claim", description="Take over a channel whose owner left")
    async def s_claim(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, await self.claim(interaction.user), ephemeral=True)

    # ---- Components V2 panel, with the banner attached to each posted panel
    @staticmethod
    def panel_file() -> discord.File:
        return discord.File(JTC_BANNER_PATH, filename=JTC_BANNER_FILENAME)

    async def panel_action(self, interaction: discord.Interaction, key: str) -> None:
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await ui.respond(interaction, ui.warn("Server Only", "Use this panel inside a server."), ephemeral=True)
            return
        channel, problem = self.temp_channel(member, claim=(key == "claim"))
        if problem:
            await ui.respond(interaction, problem, ephemeral=True)
            return
        if key in USER_ACTIONS:
            title, text = USER_ACTIONS[key]
            picker = UserPickView(self, key, channel.id, member.id)
            await interaction.response.send_message(
                embed=ui.info(title, text),
                view=picker,
                ephemeral=True,
            )
            try:
                picker.message = await interaction.original_response()
            except discord.HTTPException as error:
                print(f"[jtc] could not track member picker message: {type(error).__name__}: {error}")
        elif key in MODAL_ACTIONS:
            await interaction.response.send_modal(PanelModal(self, key, channel.id))
        else:
            await ui.respond(interaction, await self.simple_action(member, channel, key), ephemeral=True)

    async def simple_action(self, member: discord.Member, channel: discord.VoiceChannel, key: str) -> discord.Embed:
        everyone = member.guild.default_role
        reason = f"Join To Create panel: {key} by {member}"
        try:
            if key == "claim":
                return await self.claim(member)
            if key == "lock":
                await channel.set_permissions(everyone, connect=False, reason=reason)
                return ui.success("Channel Locked", f"{channel.mention} is locked. Use Permit or Invite to let someone in.")
            if key == "unlock":
                await channel.set_permissions(everyone, connect=None, reason=reason)
                return ui.success("Channel Unlocked", f"{channel.mention} is open again.")
            if key == "hide":
                await channel.set_permissions(everyone, view_channel=False, reason=reason)
                return ui.success("Channel Hidden", f"{channel.mention} is hidden from everyone except invited members.")
            if key == "unhide":
                await channel.set_permissions(everyone, view_channel=None, reason=reason)
                return ui.success("Channel Visible", f"{channel.mention} is visible again.")
            if key == "permitall":
                await channel.set_permissions(everyone, connect=True, view_channel=True, reason=reason)
                return ui.success("Everyone Permitted", f"Everyone can see and join {channel.mention}.")
        except discord.Forbidden:
            return ui.error("Missing Permissions", "I could not edit that channel. Check my Manage Channels and Manage Roles permissions.")
        except discord.HTTPException as error:
            print(f"[jtc] {key} failed for channel {channel.id}: {type(error).__name__}: {error}")
            return ui.error("Action Failed", "Discord could not update this channel. Please try again.")
        return ui.warn("Unknown Action", "That button is not available.")

    async def modal_action(self, interaction: discord.Interaction, key: str, channel_id: int, value: str) -> None:
        member = interaction.user
        channel, problem = self.temp_channel(member) if isinstance(member, discord.Member) else (None, ui.warn("Server Only", "Use this inside a server."))
        if problem is None and channel.id != channel_id:
            problem = ui.warn("Channel Changed", "Use the buttons again from inside your channel.")
        if problem:
            await ui.respond(interaction, problem, ephemeral=True)
            return
        reason = f"Join To Create panel: {key} by {member}"
        try:
            if key == "rename":
                if not value:
                    raise ValueError
                await channel.edit(name=value[:100], reason=reason)
                emb = ui.success("Channel Renamed", f"Your channel is now called {channel.mention}.")
            elif key == "limit":
                number = max(0, min(int(value), 99))
                await channel.edit(user_limit=number, reason=reason)
                emb = ui.success("Limit Updated", f"{channel.mention} now allows **{number or 'unlimited'}** members.")
            else:
                maximum = max(8, interaction.guild.bitrate_limit // 1000)
                kbps = max(8, min(int(value), maximum))
                await channel.edit(bitrate=kbps * 1000, reason=reason)
                emb = ui.success("Bitrate Updated", f"{channel.mention} now uses **{kbps} kbps** (this server allows up to {maximum} kbps).")
        except ValueError:
            emb = ui.warn("Invalid Value", "Please enter a valid number." if key != "rename" else "Please enter a name.")
        except discord.Forbidden:
            emb = ui.error("Missing Permissions", "I could not edit that channel.")
        except discord.HTTPException as error:
            print(f"[jtc] {key} update failed for channel {channel.id}: {type(error).__name__}: {error}")
            emb = ui.error("Update Failed", "Discord could not update this channel. Please try again.")
        await ui.respond(interaction, emb, ephemeral=True)

    async def user_action(self, interaction: discord.Interaction, key: str, channel_id: int, target) -> None:
        guild = interaction.guild
        owner = interaction.user
        if guild is None or not isinstance(owner, discord.Member):
            await ui.respond(interaction, ui.warn("Server Only", "Use this control inside a server."), ephemeral=True)
            return
        channel, problem = self.temp_channel(owner)
        if problem is None and channel.id != channel_id:
            problem = ui.warn("Channel Changed", "Use the buttons again from inside your channel.")
        member = guild.get_member(target.id) if problem is None else None
        if problem is None and member is None:
            problem = ui.warn("Member Not Found", "That member is not in this server.")
        if problem is None and member.bot:
            problem = ui.warn("Not Allowed", "This cannot be used on bots.")
        if problem is None and member.id == owner.id and key != "permit":
            problem = ui.warn("Not Allowed", "You cannot use this on yourself.")
        if problem is not None:
            await interaction.response.edit_message(embed=problem, view=None)
            return

        reason = f"Join To Create panel: {key} by {owner}"
        try:
            if key == "transfer":
                if member.voice is None or member.voice.channel != channel:
                    emb = ui.warn("Member Not In Channel", f"{member.mention} must be inside {channel.mention} to receive it.")
                else:
                    await channel.set_permissions(member, view_channel=True, connect=True, speak=True, reason=reason)
                    await channel.set_permissions(owner, overwrite=None, reason=reason)
                    self.cfg(guild.id)["temp"][str(channel.id)] = member.id
                    self.save()
                    emb = ui.success("Ownership Transferred", f"{member.mention} is now the owner of {channel.mention}.")
            elif key == "invite":
                await channel.set_permissions(member, view_channel=True, connect=True, reason=reason)
                link = ""
                invite_note = ""
                try:
                    invite = await channel.create_invite(max_age=600, max_uses=1, unique=True, reason=reason)
                    link = f"\n[Join now]({invite.url})"
                except discord.Forbidden:
                    invite_note = "\nDiscord could not create an invite link, but channel access was granted."
                except discord.HTTPException as error:
                    print(f"[jtc] invite link failed for channel {channel.id}: {type(error).__name__}: {error}")
                    invite_note = "\nDiscord could not create an invite link, but channel access was granted."
                sent = True
                try:
                    await member.send(
                        embed=build_notice_dm(
                            title="Voice channel invite",
                            message=(
                                f"{owner.display_name} invited you to {channel.name} "
                                f"in {guild.name}.{link}"
                            ),
                            emojis=moderation_emojis_for(self.bot, guild.id),
                            icon_key="UNLOCK",
                            color=0x8054F6,
                            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                        )
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(f"[jtc] invite DM failed for member {member.id}: {type(error).__name__}: {error}")
                    sent = False
                emb = ui.success(
                    "Member Invited",
                    f"{member.mention} can now join {channel.mention}."
                    + ("" if sent else "\nTheir DMs are closed, tell them yourself.")
                    + invite_note,
                )
            elif key == "permit":
                await channel.set_permissions(member, view_channel=True, connect=True, reason=reason)
                emb = ui.success("Member Permitted", f"{member.mention} can join {channel.mention} even when it is locked.")
            elif key == "vcban":
                await channel.set_permissions(member, view_channel=False, connect=False, reason=reason)
                if member.voice is not None and member.voice.channel == channel:
                    await member.move_to(None, reason=reason)
                emb = ui.success("Member Banned", f"{member.mention} is banned from {channel.mention}.")
            elif key == "vcunban":
                await channel.set_permissions(member, overwrite=None, reason=reason)
                emb = ui.success("Member Unbanned", f"{member.mention} can use {channel.mention} again.")
            elif key == "mute":
                await channel.set_permissions(member, speak=False, reason=reason)
                emb = ui.success("Member Muted", f"{member.mention} cannot speak in {channel.mention}.")
            else:
                await channel.set_permissions(member, speak=None, reason=reason)
                emb = ui.success("Member Unmuted", f"{member.mention} can speak in {channel.mention} again.")
        except discord.Forbidden:
            emb = ui.error("Missing Permissions", "I could not edit that channel. Check my Manage Channels and Manage Roles permissions.")
        except discord.HTTPException as error:
            print(f"[jtc] {key} failed for channel {channel.id}: {type(error).__name__}: {error}")
            emb = ui.error("Action Failed", "Discord could not update this channel. Please try again.")
        await interaction.response.edit_message(embed=emb, view=None)

    @jtc_prefix.command(name="panel", brief="Post the interface buttons (admin): .jtc panel [#channel]")
    @commands.guild_only()
    @admin_prefix
    async def jtc_panel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        try:
            await channel.send(file=self.panel_file(), view=JtcPanel(self))
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(embed=ui.error("Missing Permissions", f"I cannot send messages in {channel.mention}."))
            return
        await ctx.send(embed=ui.success("Panel Posted", f"The interface panel was posted in {channel.mention}."))

    @jtc.command(name="panel", description="Post the Join To Create interface buttons (admin)")
    @app_commands.default_permissions(administrator=True)
    @admin_slash
    async def s_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or interaction.channel
        try:
            await channel.send(file=self.panel_file(), view=JtcPanel(self))
        except (discord.Forbidden, discord.HTTPException):
            await ui.respond(interaction, ui.error("Missing Permissions", f"I cannot send messages in {channel.mention}."), ephemeral=True)
            return
        await ui.respond(interaction, ui.success("Panel Posted", f"The interface panel was posted in {channel.mention}."), ephemeral=True)
