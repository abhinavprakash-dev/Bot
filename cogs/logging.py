import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

import discord
from discord.ext import commands


class LoggingCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        state: dict[str, Any],
        has_tier: Callable[[int, str], bool],
        default_log_channel_id: int = 0,
    ) -> None:
        self.bot = bot
        self.state = state
        self.has_tier = has_tier
        self.default_log_channel_id = default_log_channel_id
        self.voice_sessions: dict[tuple[int, int], datetime] = {}
        self.destructive_actions: defaultdict[tuple[int, int], deque[float]] = defaultdict(deque)

    def get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        guild_settings = self.state["settings"].get(str(guild.id), {})
        channel_id = guild_settings.get("log_channel_id", self.default_log_channel_id)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def suite_handles(self, guild: discord.Guild) -> bool:
        """True when the automatic log channels (LogSuite) are set up for this guild."""
        suite = self.bot.get_cog("Logs")
        return suite is not None and suite.configured(guild)

    async def write_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        suite = self.bot.get_cog("Logs")
        if suite is not None and suite.configured(guild):
            await suite.send(guild, "antinuke", embed)
            return
        channel = self.get_log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print(f"Missing permission to write logs in guild {guild.id}.")
        except discord.HTTPException as error:
            print(f"Could not write an audit log in guild {guild.id}: {type(error).__name__}.")

    @staticmethod
    def format_elapsed(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    async def find_audit_executor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
    ) -> discord.User | discord.Member | None:
        try:
            async for entry in guild.audit_logs(limit=20, action=action):
                target = entry.target
                if target is not None and target.id == target_id:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def log_destructive_action(
        self,
        guild: discord.Guild,
        label: str,
        target: discord.abc.Snowflake,
        audit_action: discord.AuditLogAction,
        executor: discord.User | discord.Member | None = None,
    ) -> None:
        if not self.has_tier(guild.id, "prime") or self.suite_handles(guild):
            return
        if executor is None:
            executor = await self.find_audit_executor(guild, audit_action, target.id)

        detail = f"Target: {target} (`{target.id}`)"
        color = discord.Color.orange()
        if executor is not None:
            detail += f"\nExecutor: {executor.mention} (`{executor.id}`)"
            guild_settings = self.state["settings"].get(str(guild.id), {})
            whitelist = set(guild_settings.get("antinuke_whitelist", []))
            if executor.id in whitelist:
                detail += "\nExecutor is whitelisted."
            else:
                now = time.monotonic()
                actions = self.destructive_actions[(guild.id, executor.id)]
                actions.append(now)
                window_seconds = guild_settings.get("antinuke_window_seconds", 15)
                while actions and now - actions[0] > window_seconds:
                    actions.popleft()
                threshold = guild_settings.get("antinuke_threshold", 3)
                if guild_settings.get("antinuke_enabled") and len(actions) >= threshold:
                    detail += (
                        f"\nPossible nuke burst: {len(actions)} destructive actions "
                        f"in {window_seconds} seconds."
                    )
                    color = discord.Color.red()
        else:
            detail += "\nExecutor: unknown. Check the bot's View Audit Log permission."

        embed = discord.Embed(title=f"Server security log: {label}", description=detail, color=color)
        await self.write_log(guild, embed)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or before.channel == after.channel or not self.has_tier(member.guild.id, "prime") or self.suite_handles(member.guild):
            return

        now = datetime.now(timezone.utc)
        if before.channel is not None:
            started = self.voice_sessions.pop((member.guild.id, member.id), now)
            elapsed = self.format_elapsed((now - started).total_seconds())
            embed = discord.Embed(
                title="Voice channel left",
                description=f"{member.mention} left **{before.channel.name}** after `{elapsed}`.",
                color=discord.Color.red(),
                timestamp=now,
            )
            await self.write_log(member.guild, embed)

        if after.channel is not None:
            self.voice_sessions[(member.guild.id, member.id)] = now
            embed = discord.Embed(
                title="Voice channel joined",
                description=f"{member.mention} joined **{after.channel.name}**.",
                color=discord.Color.green(),
                timestamp=now,
            )
            await self.write_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_destructive_action(
            channel.guild, "Channel created", channel, discord.AuditLogAction.channel_create
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_destructive_action(
            channel.guild, "Channel deleted", channel, discord.AuditLogAction.channel_delete
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.log_destructive_action(role.guild, "Role created", role, discord.AuditLogAction.role_create)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.log_destructive_action(role.guild, "Role deleted", role, discord.AuditLogAction.role_delete)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log_destructive_action(guild, "Member banned", user, discord.AuditLogAction.ban)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # The kick lookup is a full audit-log API round trip, so it must not run
        # for every leave. The same cheap guards log_destructive_action uses are
        # checked here first; a normal leave (or a server without the log suite)
        # then costs nothing instead of a network request per departure.
        if not self.has_tier(member.guild.id, "prime") or self.suite_handles(member.guild):
            return
        executor = await self.find_audit_executor(member.guild, discord.AuditLogAction.kick, member.id)
        if executor is not None:
            await self.log_destructive_action(
                member.guild,
                "Member kicked",
                member,
                discord.AuditLogAction.kick,
                executor=executor,
            )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        old_code = getattr(before, "vanity_url_code", None)
        new_code = getattr(after, "vanity_url_code", None)
        if old_code == new_code or not self.has_tier(after.id, "prime"):
            return

        owner = after.owner
        owner_text = owner.mention if owner is not None else "the server owner"
        embed = discord.Embed(
            title="Vanity URL changed",
            description=(
                f"Old code: `{old_code or 'none'}`\nNew code: `{new_code or 'none'}`\n"
                f"{owner_text}, review this change. The bot cannot guarantee recovery."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await self.write_log(after, embed)