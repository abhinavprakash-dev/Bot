from datetime import datetime, timezone
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui

A = discord.AuditLogAction

# Plan needed for the log system. Change to "prime" to make it Prime only.
LOG_TIER = "prime"

# key -> (channel name, label, what it records)
LOG_TYPES: dict[str, tuple[str, str, str]] = {
    "bot": ("bot-log", "Bot Log", "Commands used and bots added or removed"),
    "member": ("member-log", "Member Log", "Joins, leaves, nickname and profile changes"),
    "antinuke": ("antinuke-log", "Antinuke Log", "Every protection trigger and punishment"),
    "message": ("message-log", "Message Log", "Deleted, edited and bulk deleted messages"),
    "moderation": ("moderation-log", "Moderation Log", "Bans, unbans, kicks and timeouts"),
    "voice": ("voice-log", "Voice Log", "Voice join, leave and move with exact times"),
    "role": ("role-log", "Role Log", "Role create, delete and edit"),
    "channel": ("channel-log", "Channel Log", "Channel create, delete, edit and permission changes"),
    "member_role": ("member-role-update", "Member Role Update", "Roles given to or removed from members"),
}

COLOR_GOOD = 0x2ECC71
COLOR_BAD = 0xE74C3C
COLOR_WARN = 0xF39C12
COLOR_INFO = 0x5865F2

ROLE_ACTIONS = {A.role_create: ("Role Created", COLOR_GOOD), A.role_delete: ("Role Deleted", COLOR_BAD), A.role_update: ("Role Updated", COLOR_WARN)}
CHANNEL_ACTIONS = {
    A.channel_create: ("Channel Created", COLOR_GOOD),
    A.channel_delete: ("Channel Deleted", COLOR_BAD),
    A.channel_update: ("Channel Updated", COLOR_WARN),
    A.overwrite_create: ("Channel Permission Added", COLOR_WARN),
    A.overwrite_update: ("Channel Permission Updated", COLOR_WARN),
    A.overwrite_delete: ("Channel Permission Removed", COLOR_WARN),
}
admin_only_slash = app_commands.checks.has_permissions(administrator=True)
admin_only_prefix = commands.has_guild_permissions(administrator=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime, style: str = "T") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>"


def trunc(text: Optional[str], limit: int = 1000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def fmt_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, discord.Colour):
        return f"#{value.value:06X}"
    if isinstance(value, (list, tuple, set)):
        return f"{len(value)} item(s)"
    return trunc(str(value), 100)


def diff_lines(entry: discord.AuditLogEntry) -> list[str]:
    try:
        before = dict(entry.before)
        after = dict(entry.after)
    except Exception:
        return []
    lines: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if isinstance(old, discord.Permissions) or isinstance(new, discord.Permissions):
            old_set = {name for name, on in (old or discord.Permissions.none()) if on}
            new_set = {name for name, on in (new or discord.Permissions.none()) if on}
            added, removed = sorted(new_set - old_set), sorted(old_set - new_set)
            if added:
                lines.append("**Permissions added:** " + ", ".join(p.replace("_", " ") for p in added))
            if removed:
                lines.append("**Permissions removed:** " + ", ".join(p.replace("_", " ") for p in removed))
            continue
        if key == "overwrites":
            lines.append("**Permission overwrites** were changed")
            continue
        lines.append(f"**{key.replace('_', ' ').title()}:** {fmt_value(old)} → {fmt_value(new)}")
    return lines[:12]


def log_embed(title: str, description: str, color: int, user: Optional[discord.abc.User] = None) -> discord.Embed:
    emb = discord.Embed(title=f"{ui.SPIDER} {title}", description=description, color=color, timestamp=utcnow())
    if user is not None:
        emb.set_author(name=user.display_name if hasattr(user, "display_name") else str(user), icon_url=user.display_avatar.url)
        emb.set_footer(text=f"User ID: {user.id}")
    else:
        emb.set_footer(text=f"{ui.BRAND} Logs")
    return emb


class LogSuite(commands.Cog, name="Logs"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None], has_tier: Callable[[int, str], bool]) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.has_tier = has_tier
        # (guild_id, user_id) -> (joined_at, approximate)
        self.voice_sessions: dict[tuple[int, int], tuple[datetime, bool]] = {}

    # ------------------------------------------------------------------ storage
    def guild_settings(self, guild_id: int) -> dict:
        return self.state["settings"].setdefault(str(guild_id), {})

    def channels(self, guild_id: int) -> dict[str, int]:
        return self.guild_settings(guild_id).get("log_channels", {})

    def active(self, guild: Optional[discord.Guild]) -> bool:
        return guild is not None and bool(self.channels(guild.id)) and self.has_tier(guild.id, LOG_TIER)

    def configured(self, guild: discord.Guild) -> bool:
        """Used by LoggingCog: when True this suite handles the logging for the guild."""
        return self.active(guild)

    async def send(self, guild: discord.Guild, kind: str, embed: discord.Embed) -> None:
        channel_id = self.channels(guild.id).get(kind)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -------------------------------------------------------------------- setup
    def plan_error(self, guild: discord.Guild) -> Optional[discord.Embed]:
        if self.has_tier(guild.id, LOG_TIER):
            return None
        return ui.error("Plan Required", "The automatic log system needs an active **PRIME** plan or higher.\nRun `.prime tiers` for details.")

    async def setup_logs(self, guild: discord.Guild) -> discord.Embed:
        problem = self.plan_error(guild)
        if problem is not None:
            return problem
        me = guild.me
        if me is None or not (me.guild_permissions.administrator or me.guild_permissions.manage_channels):
            return ui.error("Missing Permissions", "I need the **Manage Channels** permission to create the log channels.")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
        }
        settings = self.guild_settings(guild.id)
        mapping = dict(settings.get("log_channels", {}))
        created = 0
        try:
            category = guild.get_channel(settings.get("log_category_id", 0))
            if not isinstance(category, discord.CategoryChannel):
                category = await guild.create_category(f"{ui.BRAND} Logs", overwrites=overwrites, reason="Log system setup")
            for key, (name, label, topic) in LOG_TYPES.items():
                existing = guild.get_channel(mapping.get(key, 0))
                if isinstance(existing, discord.TextChannel):
                    continue
                channel = await category.create_text_channel(name, overwrites=overwrites, topic=topic, reason="Log system setup")
                mapping[key] = channel.id
                created += 1
        except discord.Forbidden:
            return ui.error("Missing Permissions", "Discord refused to create the channels. Check my **Manage Channels** permission and role position.")
        except discord.HTTPException as error:
            return ui.error("Setup Failed", f"Discord returned an error ({type(error).__name__}). Please try again.")

        settings["log_channels"] = mapping
        settings["log_category_id"] = category.id
        settings["log_channel_id"] = mapping["antinuke"]
        self.save()
        rows = [(label, f"<#{mapping[key]}>") for key, (_name, label, _topic) in LOG_TYPES.items()]
        return ui.panel(
            "Log System Ready",
            f"**{created}** new channel(s) created. Every event is now recorded automatically.",
            rows,
            notes=["Only administrators can see the log channels", "Use /logs set to move a log to another channel"],
            thumbnail=self.bot.user.display_avatar.url if self.bot.user else None,
        )

    def status_embed(self, guild: discord.Guild) -> discord.Embed:
        mapping = self.channels(guild.id)
        rows = [(label, f"<#{mapping[key]}>" if key in mapping else "`Not set`") for key, (_name, label, _topic) in LOG_TYPES.items()]
        state = "Active" if self.active(guild) else ("Inactive (plan required)" if mapping else "Not set up")
        return ui.panel(
            "Log System Status",
            f"Status: **{state}**",
            rows,
            notes=["Use /logs setup to create every log channel automatically"],
            thumbnail=self.bot.user.display_avatar.url if self.bot.user else None,
        )

    def set_channel(self, guild: discord.Guild, kind: str, channel: discord.TextChannel) -> discord.Embed:
        problem = self.plan_error(guild)
        if problem is not None:
            return problem
        settings = self.guild_settings(guild.id)
        mapping = dict(settings.get("log_channels", {}))
        mapping[kind] = channel.id
        settings["log_channels"] = mapping
        if kind == "antinuke":
            settings["log_channel_id"] = channel.id
        self.save()
        return ui.success("Log Channel Updated", f"**{LOG_TYPES[kind][1]}** will now be sent to {channel.mention}.")

    def disable(self, guild: discord.Guild) -> discord.Embed:
        self.guild_settings(guild.id).pop("log_channels", None)
        self.save()
        return ui.warn("Log System Disabled", "Logging was turned off. The channels were kept, run `/logs setup` to turn it on again.")

    # ---------------------------------------------------------------- commands
    logs = app_commands.Group(
        name="logs",
        description="Automatic server logs",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @logs.command(name="setup", description="Create every log channel automatically")
    @admin_only_slash
    async def logs_setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await ui.respond(interaction, await self.setup_logs(interaction.guild))

    @logs.command(name="status", description="Show where each log is sent")
    @admin_only_slash
    async def logs_status(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.status_embed(interaction.guild))

    @logs.command(name="set", description="Send one log type to an existing channel")
    @app_commands.describe(log_type="Which log to move", channel="The new channel")
    @app_commands.choices(log_type=[app_commands.Choice(name=label, value=key) for key, (_n, label, _t) in LOG_TYPES.items()])
    @admin_only_slash
    async def logs_set(self, interaction: discord.Interaction, log_type: app_commands.Choice[str], channel: discord.TextChannel) -> None:
        await ui.respond(interaction, self.set_channel(interaction.guild, log_type.value, channel))

    @logs.command(name="disable", description="Turn the log system off")
    @admin_only_slash
    async def logs_disable(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.disable(interaction.guild))

    @commands.group(name="logs", invoke_without_command=True, brief="Automatic logs: setup, status, set, off.",
                    extras={"category": "Anti Nuke", "usage": ".logs setup|status|set|off"})
    @commands.guild_only()
    @admin_only_prefix
    async def logs_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.status_embed(ctx.guild))

    @logs_prefix.command(name="setup", brief="Create every log channel automatically.")
    @commands.guild_only()
    @admin_only_prefix
    async def logs_setup_prefix(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            emb = await self.setup_logs(ctx.guild)
        await ctx.send(embed=emb)

    @logs_prefix.command(name="status", brief="Show where each log is sent.")
    @commands.guild_only()
    @admin_only_prefix
    async def logs_status_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.status_embed(ctx.guild))

    @logs_prefix.command(name="set", brief="Send one log type to a channel: .logs set voice #channel")
    @commands.guild_only()
    @admin_only_prefix
    async def logs_set_prefix(self, ctx: commands.Context, log_type: str, channel: discord.TextChannel) -> None:
        key = log_type.lower().replace("-", "_")
        if key not in LOG_TYPES:
            await ctx.send(embed=ui.warn("Unknown Log Type", "Use one of: " + ", ".join(f"`{k}`" for k in LOG_TYPES)))
            return
        await ctx.send(embed=self.set_channel(ctx.guild, key, channel))

    @logs_prefix.command(name="off", brief="Turn the log system off.")
    @commands.guild_only()
    @admin_only_prefix
    async def logs_off_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.disable(ctx.guild))

    # -------------------------------------------------------------- voice log
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        moment = utcnow()
        for guild in self.bot.guilds:
            for voice in guild.voice_channels:
                for member in voice.members:
                    self.voice_sessions.setdefault((guild.id, member.id), (moment, True))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        guild = member.guild
        if member.bot or before.channel == after.channel or not self.active(guild):
            return
        moment = utcnow()
        key = (guild.id, member.id)

        if before.channel is None and after.channel is not None:
            self.voice_sessions[key] = (moment, False)
            desc = (
                f"{member.mention} joined {after.channel.mention}\n\n"
                f"**Joined at:** {stamp(moment, 'f')} ({stamp(moment, 'R')})"
            )
            await self.send(guild, "voice", log_embed("Voice Join", desc, COLOR_GOOD, member))
            return

        if before.channel is not None and after.channel is None:
            started, approximate = self.voice_sessions.pop(key, (None, True))
            lines = [f"{member.mention} left {before.channel.mention}", ""]
            if started is not None and not approximate:
                lines.append(f"**Joined at:** {stamp(started, 'f')}")
                lines.append(f"**Left at:** {stamp(moment, 'f')}")
                lines.append(f"**Time spent:** `{duration((moment - started).total_seconds())}`")
            elif started is not None:
                lines.append("**Joined at:** before the bot started watching")
                lines.append(f"**Left at:** {stamp(moment, 'f')}")
                lines.append(f"**Time spent:** at least `{duration((moment - started).total_seconds())}`")
            else:
                lines.append("**Joined at:** unknown")
                lines.append(f"**Left at:** {stamp(moment, 'f')}")
            await self.send(guild, "voice", log_embed("Voice Leave", "\n".join(lines), COLOR_BAD, member))
            return

        if before.channel is not None and after.channel is not None:
            desc = (
                f"{member.mention} moved from {before.channel.mention} to {after.channel.mention}\n\n"
                f"**Moved at:** {stamp(moment, 'f')}"
            )
            await self.send(guild, "voice", log_embed("Voice Move", desc, COLOR_INFO, member))

    # ------------------------------------------------------------ message log
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self.active(guild):
            return
        message = payload.cached_message
        if message is not None:
            if message.author.bot:
                return
            lines = [f"**Author:** {message.author.mention}", f"**Channel:** <#{payload.channel_id}>", ""]
            lines.append(trunc(message.content, 1500) or "*No text content*")
            if message.attachments:
                lines.append("")
                lines.append("**Attachments:** " + ", ".join(a.filename for a in message.attachments[:5]))
            await self.send(guild, "message", log_embed("Message Deleted", "\n".join(lines), COLOR_BAD, message.author))
            return
        desc = f"A message was deleted in <#{payload.channel_id}>.\n**Message ID:** `{payload.message_id}`\nThe content was not cached."
        await self.send(guild, "message", log_embed("Message Deleted", desc, COLOR_BAD))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not self.active(guild):
            return
        desc = f"**{len(payload.message_ids)}** messages were bulk deleted in <#{payload.channel_id}>."
        await self.send(guild, "message", log_embed("Bulk Delete", desc, COLOR_BAD))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.author.bot or not self.active(after.guild):
            return
        previous = before.content if before is not None else None
        if previous is not None and previous == after.content:
            return
        desc = (
            f"**Author:** {after.author.mention}\n**Channel:** {after.channel.mention} · [Jump to message]({after.jump_url})\n\n"
            f"**Before**\n{trunc(previous, 900) if previous is not None else '*not cached*'}\n\n**After**\n{trunc(after.content, 900) or '*empty*'}"
        )
        await self.send(after.guild, "message", log_embed("Message Edited", trunc(desc, 4000), COLOR_WARN, after.author))

    # ------------------------------------------------------------- member log
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.active(member.guild):
            return
        desc = (
            f"{member.mention} joined the server.\n\n"
            f"**Account created:** {stamp(member.created_at, 'f')} ({stamp(member.created_at, 'R')})\n"
            f"**Member count:** {member.guild.member_count}"
        )
        await self.send(member.guild, "member", log_embed("Member Joined", desc, COLOR_GOOD, member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self.active(member.guild):
            return
        if member.bot:
            await self.send(member.guild, "bot", log_embed("Bot Removed", f"{member.mention} (`{member.id}`) is no longer in the server.", COLOR_BAD, member))
            return
        roles = [role.mention for role in member.roles if not role.is_default()]
        joined = stamp(member.joined_at, "f") if member.joined_at else "unknown"
        desc = (
            f"{member.mention} left the server.\n\n**Joined:** {joined}\n"
            f"**Roles:** {trunc(' '.join(roles), 900) or 'none'}\n**Member count:** {member.guild.member_count}"
        )
        await self.send(member.guild, "member", log_embed("Member Left", desc, COLOR_BAD, member))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or before.nick == after.nick or not self.active(after.guild):
            return
        desc = f"{after.mention} changed their nickname.\n\n**Before:** {before.nick or 'none'}\n**After:** {after.nick or 'none'}"
        await self.send(after.guild, "member", log_embed("Nickname Changed", desc, COLOR_WARN, after))

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if after.bot:
            return
        changes = []
        if before.name != after.name:
            changes.append(f"**Username:** {before.name} → {after.name}")
        if before.display_avatar != after.display_avatar:
            changes.append("**Avatar** was changed")
        if not changes:
            return
        try:
            guilds = after.mutual_guilds
        except Exception:
            return
        for guild in guilds:
            if self.active(guild):
                await self.send(guild, "member", log_embed("Profile Updated", f"{after.mention}\n\n" + "\n".join(changes), COLOR_INFO, after))

    # -------------------------------------------------------------- bot log
    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if ctx.guild is None or not self.active(ctx.guild):
            return
        desc = f"{ctx.author.mention} used `{trunc(ctx.message.content, 200)}` in {ctx.channel.mention}"
        await self.send(ctx.guild, "bot", log_embed("Command Used", desc, COLOR_INFO, ctx.author))

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: Any) -> None:
        if interaction.guild is None or not self.active(interaction.guild):
            return
        where = interaction.channel.mention if getattr(interaction.channel, "mention", None) else "a channel"
        desc = f"{interaction.user.mention} used `/{command.qualified_name}` in {where}"
        await self.send(interaction.guild, "bot", log_embed("Slash Command Used", desc, COLOR_INFO, interaction.user))

    # ---------------------------------------------- audit log driven logs
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        if not self.active(guild):
            return
        action = entry.action
        mine = self.bot.user is not None and entry.user_id == self.bot.user.id
        executor = f"<@{entry.user_id}>" if entry.user_id else "Unknown"
        executor_user = guild.get_member(entry.user_id) if entry.user_id else None
        target_id = getattr(entry.target, "id", None)

        if action in ROLE_ACTIONS and not mine:
            title, color = ROLE_ACTIONS[action]
            after, before = dict(entry.after), dict(entry.before)
            name = after.get("name") or before.get("name") or getattr(entry.target, "name", None) or str(target_id)
            role = guild.get_role(target_id) if target_id else None
            shown = role.mention if role is not None else f"**{name}**"
            desc = f"**Role:** {shown}\n**By:** {executor}"
            if action is A.role_update:
                desc += "\n\n" + "\n".join(diff_lines(entry))
            await self.send(guild, "role", log_embed(title, trunc(desc, 4000), color, executor_user))

        elif action in CHANNEL_ACTIONS and not mine:
            title, color = CHANNEL_ACTIONS[action]
            after, before = dict(entry.after), dict(entry.before)
            name = after.get("name") or before.get("name") or getattr(entry.target, "name", None) or str(target_id)
            channel = guild.get_channel(target_id) if target_id else None
            shown = channel.mention if channel is not None else f"**{name}**"
            desc = f"**Channel:** {shown}\n**By:** {executor}"
            if action in (A.overwrite_create, A.overwrite_update, A.overwrite_delete):
                extra = entry.extra
                desc += f"\n**For:** {getattr(extra, 'mention', None) or '`' + str(getattr(extra, 'id', '?')) + '`'}"
            elif action is A.channel_update:
                desc += "\n\n" + "\n".join(diff_lines(entry))
            await self.send(guild, "channel", log_embed(title, trunc(desc, 4000), color, executor_user))

        elif action is A.member_role_update and not mine:
            added = [f"<@&{role.id}>" for role in dict(entry.after).get("roles", [])]
            removed = [f"<@&{role.id}>" for role in dict(entry.before).get("roles", [])]
            if not added and not removed:
                return
            member = guild.get_member(target_id) if target_id else None
            desc = f"**Member:** <@{target_id}>\n**By:** {executor}"
            if added:
                desc += f"\n**Roles added:** {' '.join(added)}"
            if removed:
                desc += f"\n**Roles removed:** {' '.join(removed)}"
            await self.send(guild, "member_role", log_embed("Member Roles Updated", desc, COLOR_WARN, member))

        elif action is A.bot_add:
            desc = f"<@{target_id}> (`{target_id}`) was added to the server.\n**Added by:** {executor}"
            await self.send(guild, "bot", log_embed("Bot Added", desc, COLOR_WARN, executor_user))

        elif action in (A.ban, A.unban, A.kick):
            title, color = {A.ban: ("Member Banned", COLOR_BAD), A.unban: ("Member Unbanned", COLOR_GOOD), A.kick: ("Member Kicked", COLOR_BAD)}[action]
            desc = f"**Member:** <@{target_id}> (`{target_id}`)\n**Moderator:** {executor}\n**Reason:** {trunc(entry.reason, 500) or 'No reason given'}"
            await self.send(guild, "moderation", log_embed(title, desc, color, executor_user))

        elif action is A.member_prune:
            removed_count = getattr(entry.extra, "members_removed", "?")
            desc = f"**{removed_count}** inactive members were pruned.\n**Moderator:** {executor}"
            await self.send(guild, "moderation", log_embed("Members Pruned", desc, COLOR_BAD, executor_user))

        elif action is A.member_update:
            changes = dict(entry.after)
            if "timed_out_until" not in changes:
                return
            until = changes["timed_out_until"]
            if until is None:
                title, color, extra_line = "Timeout Removed", COLOR_GOOD, ""
            else:
                title, color, extra_line = "Member Timed Out", COLOR_BAD, f"\n**Until:** {stamp(until, 'f')} ({stamp(until, 'R')})"
            desc = f"**Member:** <@{target_id}>\n**Moderator:** {executor}{extra_line}\n**Reason:** {trunc(entry.reason, 500) or 'No reason given'}"
            await self.send(guild, "moderation", log_embed(title, desc, color, executor_user))
