from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui


log = logging.getLogger(__name__)
DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS = 7


class InviteTrackerCog(commands.Cog, name="InviteTrackerCog"):
    def __init__(
        self,
        bot: commands.Bot,
        state: dict[str, Any],
        save: Callable[[], None],
    ) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.invite_cache: dict[int, dict[str, tuple[int, int | None]]] = {}
        self._guild_locks: defaultdict[int, Any] = defaultdict(asyncio.Lock)

    def cfg(self, guild_id: int) -> dict[str, Any]:
        cfg = self.state.setdefault("settings", {}).setdefault(str(guild_id), {}).setdefault("invite_tracker", {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("log_channel_id", 0)
        cfg.setdefault("minimum_account_age_days", DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS)
        cfg.setdefault("members", {})
        if not isinstance(cfg["members"], dict):
            cfg["members"] = {}
        return cfg

    @staticmethod
    def snapshot(invites: list[discord.Invite]) -> dict[str, tuple[int, int | None]]:
        return {
            invite.code: (
                invite.uses or 0,
                invite.inviter.id if invite.inviter is not None else None,
            )
            for invite in invites
        }

    async def refresh_guild(self, guild: discord.Guild) -> None:
        cfg = self.cfg(guild.id)
        if not cfg["enabled"]:
            return
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("Invite tracker could not fetch invites for guild %s: %s", guild.id, error)
            return
        async with self._guild_locks[guild.id]:
            self.invite_cache[guild.id] = self.snapshot(invites)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if self.cfg(guild.id)["enabled"]:
                await self.refresh_guild(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None or not self.cfg(invite.guild.id)["enabled"]:
            return
        async with self._guild_locks[invite.guild.id]:
            self.invite_cache.setdefault(invite.guild.id, {})[invite.code] = (
                invite.uses or 0,
                invite.inviter.id if invite.inviter is not None else None,
            )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is not None:
            async with self._guild_locks[invite.guild.id]:
                self.invite_cache.setdefault(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.cfg(member.guild.id)["enabled"]:
            return
        guild = member.guild
        cfg = self.cfg(guild.id)
        inviter_id = None
        invite_code = None
        async with self._guild_locks[guild.id]:
            previous = self.invite_cache.get(guild.id)
            try:
                current_invites = await guild.invites()
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning("Invite tracker could not resolve a join in guild %s: %s", guild.id, error)
                current_invites = None
            if current_invites is not None:
                current = self.snapshot(current_invites)
                if previous is not None:
                    increased = [
                        (uses - previous.get(code, (0, None))[0], code, inviter)
                        for code, (uses, inviter) in current.items()
                        if uses > previous.get(code, (0, None))[0] and inviter is not None
                    ]
                    if len(increased) == 1:
                        _, invite_code, inviter_id = increased[0]
                self.invite_cache[guild.id] = current

        minimum_age = cfg.get("minimum_account_age_days", DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS)
        if type(minimum_age) is not int or not 0 <= minimum_age <= 90:
            minimum_age = DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS
        age_days = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 86400
        suspicious = minimum_age > 0 and age_days < minimum_age
        record = {
            "inviter_id": inviter_id,
            "invite_code": invite_code,
            "status": "active",
            "suspicious": suspicious,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }
        cfg["members"][str(member.id)] = record
        self.save()
        await self.log_join(member, record)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        cfg = self.cfg(member.guild.id)
        if not cfg["enabled"]:
            return
        record = cfg["members"].get(str(member.id))
        if record is None or record.get("status") != "active":
            return
        record["status"] = "left"
        self.save()
        channel = self.log_channel(member.guild)
        if channel is not None:
            try:
                await channel.send(embed=ui.warn(
                    "Tracked Invite Left",
                    f"{member.mention} left. Their invite is no longer counted as active.",
                ))
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning("Invite tracker could not log a leave in guild %s: %s", member.guild.id, error)

    async def log_join(self, member: discord.Member, record: dict[str, Any]) -> None:
        channel = self.log_channel(member.guild)
        if channel is None:
            return
        inviter = member.guild.get_member(record["inviter_id"]) if record["inviter_id"] else None
        if record["suspicious"]:
            category = "Suspicious account"
        elif inviter is not None:
            category = "Invite tracked"
        else:
            category = "Invite source unknown"
        description = f"{member.mention} joined."
        if inviter is not None:
            description += f"\nInvited by {inviter.mention}."
        if record["suspicious"]:
            age_days = max(0, (datetime.now(timezone.utc) - member.created_at).days)
            description += f"\nAccount age: {age_days} days; this invite is flagged as suspicious."
        try:
            await channel.send(embed=ui.info(category, description))
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("Invite tracker could not log a join in guild %s: %s", member.guild.id, error)

    def log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self.cfg(guild.id).get("log_channel_id", 0)
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def counts(self, guild: discord.Guild, inviter_id: int) -> dict[str, int]:
        records = self.cfg(guild.id)["members"].values()
        related = [record for record in records if record.get("inviter_id") == inviter_id]
        fake = sum(bool(record.get("suspicious")) for record in related)
        left = sum(record.get("status") == "left" and not record.get("suspicious") for record in related)
        active = sum(record.get("status") == "active" and not record.get("suspicious") for record in related)
        return {"active": active, "left": left, "fake": fake, "total": active + left + fake}

    def leaderboard(self, guild: discord.Guild, limit: int = 10) -> list[dict[str, Any]]:
        inviter_ids = {
            record.get("inviter_id")
            for record in self.cfg(guild.id)["members"].values()
            if record.get("inviter_id") is not None
        }
        entries = []
        for inviter_id in inviter_ids:
            counts = self.counts(guild, inviter_id)
            if counts["total"] == 0:
                continue
            member = guild.get_member(inviter_id)
            entries.append({
                "id": str(inviter_id),
                "name": member.display_name if member is not None else f"User {inviter_id}",
                **counts,
            })
        return sorted(entries, key=lambda item: (-item["active"], -item["total"], item["name"].lower()))[:limit]

    def dashboard_payload(self, guild: discord.Guild, user_id: int | None = None) -> dict[str, Any]:
        cfg = self.cfg(guild.id)
        me = guild.me
        tracking_available = bool(me and me.guild_permissions.manage_guild)
        minimum_age = cfg.get("minimum_account_age_days", DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS)
        if type(minimum_age) is not int or not 0 <= minimum_age <= 90:
            minimum_age = DEFAULT_MINIMUM_ACCOUNT_AGE_DAYS
        own_counts = self.counts(guild, user_id) if user_id is not None else {
            "active": 0, "left": 0, "fake": 0, "total": 0,
        }
        return {
            "enabled": bool(cfg["enabled"]),
            "log_channel_id": str(cfg["log_channel_id"] or ""),
            "minimum_account_age_days": minimum_age,
            "tracking_available": tracking_available,
            "your_invites": own_counts,
            "leaderboard": self.leaderboard(guild),
        }

    def member_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        counts = self.counts(guild, member.id)
        total = counts["total"]
        active_share = round(counts["active"] / total * 100) if total else 0
        embed = ui.security(
            f"Invites · {member.display_name}",
            f"{member.mention}  ·  Invite performance overview",
        )
        embed.set_author(
            name=f"{guild.name}  •  INVITE TRACKER",
            icon_url=guild.icon.url if guild.icon else None,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ACTIVE", value=f"**{counts['active']}**\nValid invitees", inline=True)
        embed.add_field(name="LEFT", value=f"**{counts['left']}**\nInvitees who left", inline=True)
        embed.add_field(name="FLAGGED", value=f"**{counts['fake']}**\nNew accounts", inline=True)
        embed.add_field(
            name="TRACKED CONVERSION",
            value=f"**{active_share}%** active  ·  {total} total tracked joins",
            inline=False,
        )
        embed.set_footer(text=f"KRYO  •  INVITE TRACKER  •  {guild.member_count or 0:,} server members")
        return embed

    def leaderboard_embed(self, guild: discord.Guild) -> discord.Embed:
        entries = self.leaderboard(guild)
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for index, entry in enumerate(entries, 1):
            rank = medals[index - 1] if index <= len(medals) else f"`#{index:02}`"
            lines.append(
                f"{rank}  **{entry['name']}**\n"
                f"　`{entry['active']} active`  ·  {entry['left']} left  ·  {entry['fake']} flagged"
            )
        embed = ui.gold(
            "Invite Leaderboard",
            "\n\n".join(lines) or "No invite activity has been tracked yet.",
        )
        embed.set_author(
            name=f"{guild.name}  •  COMMUNITY RANKINGS",
            icon_url=guild.icon.url if guild.icon else None,
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url if self.bot.user else None)
        embed.add_field(name="RANKED BY", value="Active, valid invitees", inline=True)
        embed.add_field(name="SHOWING", value=f"Top {len(entries)} inviters", inline=True)
        embed.set_footer(text="KRYO  •  INVITE TRACKER  •  Updated live")
        return embed

    @commands.group(
        name="invites",
        invoke_without_command=True,
        brief="Show tracked invite counts.",
        extras={"category": "Invites", "usage": ".invites [member] | .invites leaderboard"},
    )
    @commands.guild_only()
    async def invites_prefix(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not self.cfg(ctx.guild.id)["enabled"]:
            await ctx.send(embed=ui.warn("Invite Tracker Off", "A server admin can enable invite tracking in the KRYO dashboard."))
            return
        await ctx.send(embed=self.member_embed(ctx.guild, member or ctx.author))

    @invites_prefix.command(name="leaderboard", brief="Show the invite leaderboard.")
    @commands.guild_only()
    async def invites_leaderboard_prefix(self, ctx: commands.Context) -> None:
        if not self.cfg(ctx.guild.id)["enabled"]:
            await ctx.send(embed=ui.warn("Invite Tracker Off", "A server admin can enable invite tracking in the KRYO dashboard."))
            return
        await ctx.send(embed=self.leaderboard_embed(ctx.guild))

    invite_commands = app_commands.Group(name="invites", description="Tracked invite counts", guild_only=True)

    @invite_commands.command(name="user", description="Show tracked invite counts for a member")
    async def invites_slash(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not self.cfg(guild.id)["enabled"]:
            await interaction.response.send_message(
                embed=ui.warn("Invite Tracker Off", "A server admin can enable invite tracking in the KRYO dashboard."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=self.member_embed(guild, member or interaction.user))

    @invite_commands.command(name="leaderboard", description="Show the invite leaderboard")
    async def invites_leaderboard_slash(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not self.cfg(guild.id)["enabled"]:
            await interaction.response.send_message(
                embed=ui.warn("Invite Tracker Off", "A server admin can enable invite tracking in the KRYO dashboard."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=self.leaderboard_embed(guild))
