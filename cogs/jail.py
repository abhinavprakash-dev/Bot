from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands


log = logging.getLogger(__name__)
JAILED_CHANNEL_PERMISSIONS = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "add_reactions",
    "connect",
    "speak",
    "stream",
    "use_voice_activation",
    "request_to_speak",
    "use_soundboard",
    "use_external_sounds",
    "use_embedded_activities",
    "send_voice_messages",
    "use_application_commands",
)


class JailActionError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class JailCog(commands.Cog, name="JailCog"):
    def __init__(
        self,
        bot: commands.Bot,
        state: dict[str, Any],
        save: Callable[[], None],
    ) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self._ready = False

    def cfg(self, guild_id: int) -> dict[str, Any]:
        settings = self.state.setdefault("settings", {}).setdefault(str(guild_id), {})
        cfg = settings.setdefault("jail", {})
        cfg.setdefault("role_id", 0)
        cfg.setdefault("members", {})
        if not isinstance(cfg["members"], dict):
            cfg["members"] = {}
        return cfg

    def payload(self, guild: discord.Guild) -> dict[str, Any]:
        cfg = self.cfg(guild.id)
        role = guild.get_role(int(cfg["role_id"])) if str(cfg["role_id"]).isdigit() else None
        members = [
            {
                "id": str(member_id),
                "name": str(record.get("name", "Unknown member")),
                "jailed_at": str(record.get("jailed_at", "")),
                "reason": str(record.get("reason", "")),
            }
            for member_id, record in cfg["members"].items()
            if isinstance(record, dict)
        ]
        members.sort(key=lambda member: member["name"].casefold())
        me = guild.me
        return {
            "role_id": str(role.id) if role is not None else "",
            "role_name": role.name if role is not None else "",
            "members": members,
            "manage_roles_available": bool(me and me.guild_permissions.manage_roles),
        }

    async def ensure_role(self, guild: discord.Guild, cfg: dict[str, Any]) -> discord.Role:
        raw_role_id = cfg.get("role_id", 0)
        role = guild.get_role(int(raw_role_id)) if str(raw_role_id).isdigit() else None
        if role is not None:
            return role
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            raise JailActionError("KRYO needs Manage Roles permission to create the Jail role and restrict members.", 403)
        try:
            role = await guild.create_role(
                name="KRYO Jail",
                permissions=discord.Permissions.none(),
                hoist=False,
                mentionable=False,
                reason="Create the KRYO jail role",
            )
        except discord.Forbidden as error:
            raise JailActionError("KRYO cannot create the Jail role. Move its highest role above the role position it can manage and grant Manage Roles.", 403) from error
        except discord.HTTPException as error:
            log.warning("Could not create jail role for guild %s: %s", guild.id, error)
            raise JailActionError("Discord could not create the Jail role.", 502) from error
        cfg["role_id"] = role.id
        self.save()
        return role

    @staticmethod
    def find_member(guild: discord.Guild, member_id: int) -> discord.Member | None:
        return guild.get_member(member_id)

    async def resolve_member(self, guild: discord.Guild, member_id: int) -> discord.Member:
        member = self.find_member(guild, member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except discord.NotFound as error:
            raise JailActionError("That Discord account is not a member of this server.", 404) from error
        except discord.Forbidden as error:
            raise JailActionError("KRYO cannot look up that server member.", 403) from error
        except discord.HTTPException as error:
            log.warning("Could not fetch jail target %s in guild %s: %s", member_id, guild.id, error)
            raise JailActionError("Discord could not load that server member.", 502) from error

    async def resolve_member_reference(
        self,
        guild: discord.Guild,
        reference: str | int,
    ) -> discord.Member:
        if isinstance(reference, int) and not isinstance(reference, bool):
            return await self.resolve_member(guild, reference)
        value = str(reference).strip()
        mention = re.fullmatch(r"<@!?(\d{17,20})>", value)
        if mention:
            value = mention.group(1)
        if value.isdigit():
            if not 17 <= len(value) <= 20:
                raise JailActionError("Enter a valid Discord member ID.", 400)
            return await self.resolve_member(guild, int(value))
        if not value or len(value) > 100:
            raise JailActionError("Enter a Discord username or member ID.", 400)

        query = value.removeprefix("@").casefold()
        candidates = list(getattr(guild, "members", ()))

        def exact_matches(members: list[discord.Member]) -> list[discord.Member]:
            usernames = [
                member for member in members
                if str(getattr(member, "name", "")).casefold() == query
            ]
            if usernames:
                return usernames
            return [
                member for member in members
                if query in {
                    str(getattr(member, "display_name", "")).casefold(),
                    str(getattr(member, "global_name", "") or "").casefold(),
                }
            ]

        matches = exact_matches(candidates)
        if not matches:
            query_members = getattr(guild, "query_members", None)
            if callable(query_members):
                try:
                    matches = exact_matches(await query_members(query=query, limit=100))
                except discord.Forbidden as error:
                    raise JailActionError("KRYO cannot search members by username. Try their Discord ID.", 403) from error
                except discord.HTTPException as error:
                    log.warning("Could not search for jail target %r in guild %s: %s", value, guild.id, error)
                    raise JailActionError("Discord could not search members by username. Try their Discord ID.", 502) from error
        unique_matches = {member.id: member for member in matches}
        if len(unique_matches) == 1:
            return next(iter(unique_matches.values()))
        if len(unique_matches) > 1:
            raise JailActionError("That username matches multiple server members. Use the member's Discord ID.", 409)
        raise JailActionError("No server member matched that username. Try their exact username or Discord ID.", 404)

    @staticmethod
    def validate_target(guild: discord.Guild, member: discord.Member) -> None:
        me = guild.me
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            raise JailActionError("The server owner and members with Administrator permission cannot be jailed.", 403)
        if me is None or not me.guild_permissions.manage_roles:
            raise JailActionError("KRYO needs Manage Roles permission to jail members.", 403)
        if member.top_role >= me.top_role:
            raise JailActionError("KRYO's highest role must be above the member's highest role before they can be jailed.", 403)
        voice_state = getattr(member, "voice", None)
        if voice_state is not None and voice_state.channel is not None and not me.guild_permissions.move_members:
            raise JailActionError("KRYO needs Move Members permission to disconnect a member who is currently in voice.", 403)

    @staticmethod
    def member_overwrite(channel: discord.abc.GuildChannel, member: discord.Member) -> discord.PermissionOverwrite:
        return channel.overwrites_for(member)

    @staticmethod
    def permission_targets(guild: discord.Guild) -> list[discord.abc.GuildChannel]:
        return [
            channel
            for channel in guild.channels
            if isinstance(channel, discord.CategoryChannel)
            or channel.category is None
            or not channel.permissions_synced
        ]

    async def apply_channel_restriction(
        self,
        channel: discord.abc.GuildChannel,
        member: discord.Member,
        record: dict[str, Any],
    ) -> None:
        channel_id = str(channel.id)
        saved_overwrites = record.setdefault("overwrites", {})
        if channel_id not in saved_overwrites:
            current = self.member_overwrite(channel, member)
            saved_overwrites[channel_id] = {
                permission: getattr(current, permission)
                for permission in JAILED_CHANNEL_PERMISSIONS
            }
        overwrite = self.member_overwrite(channel, member)
        changed = any(
            getattr(overwrite, permission) is not False
            for permission in JAILED_CHANNEL_PERMISSIONS
        )
        overwrite.update(**{permission: False for permission in JAILED_CHANNEL_PERMISSIONS})
        if changed:
            await channel.set_permissions(
                member,
                overwrite=overwrite,
                reason="Restrict a jailed member while preserving channel visibility",
            )

    async def restore_channel_restriction(
        self,
        channel: discord.abc.GuildChannel,
        member: discord.Member,
        record: dict[str, Any],
    ) -> None:
        saved_overwrites = record.get("overwrites", {})
        original = saved_overwrites.get(str(channel.id))
        if not isinstance(original, dict):
            return
        overwrite = self.member_overwrite(channel, member)
        overwrite.update(**{
            permission: original.get(permission)
            for permission in JAILED_CHANNEL_PERMISSIONS
        })
        await channel.set_permissions(
            member,
            overwrite=None if overwrite.is_empty() else overwrite,
            reason="Restore channel permissions after a member is released from jail",
        )

    async def jail_member(
        self,
        guild: discord.Guild,
        member_id: int | str,
        reason: str,
        moderator: str = "Dashboard",
    ) -> dict[str, Any]:
        cfg = self.cfg(guild.id)
        member = await self.resolve_member_reference(guild, member_id)
        self.validate_target(guild, member)
        if str(member.id) in cfg["members"]:
            raise JailActionError("That member is already jailed.")
        role = await self.ensure_role(guild, cfg)
        if role >= guild.me.top_role:
            raise JailActionError("KRYO's highest role must be above the Jail role.", 403)

        record: dict[str, Any] = {
            "member_id": str(member.id),
            "name": member.display_name,
            "jailed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "moderator": moderator,
            "overwrites": {},
        }
        try:
            for channel in self.permission_targets(guild):
                await self.apply_channel_restriction(channel, member, record)
            voice_state = getattr(member, "voice", None)
            if voice_state is not None and voice_state.channel is not None:
                await member.move_to(None, reason=f"Jailed by {moderator}: {reason or 'No reason provided'}"[:512])
            await member.add_roles(role, reason=f"Jailed by {moderator}: {reason or 'No reason provided'}"[:512])
        except discord.Forbidden as error:
            rollback_complete = await self.rollback_jail(guild, member, record)
            if not rollback_complete:
                cfg["members"][str(member.id)] = record
                self.save()
                raise JailActionError("Jailing was only partially applied and could not be fully rolled back. The member is listed as jailed; release them after fixing the bot's permissions.", 502) from error
            raise JailActionError("KRYO cannot apply jail permissions. Check Manage Roles and channel permission overwrites.", 403) from error
        except discord.HTTPException as error:
            rollback_complete = await self.rollback_jail(guild, member, record)
            if not rollback_complete:
                cfg["members"][str(member.id)] = record
                self.save()
                raise JailActionError("Jailing was only partially applied and could not be fully rolled back. The member is listed as jailed; release them after fixing the bot's permissions.", 502) from error
            log.warning("Jailing member %s failed in guild %s: %s", member.id, guild.id, error)
            raise JailActionError("Discord could not finish applying jail permissions. No jail action was saved.", 502) from error

        cfg["members"][str(member.id)] = record
        self.save()
        return record

    async def rollback_jail(
        self,
        guild: discord.Guild,
        member: discord.Member,
        record: dict[str, Any],
    ) -> bool:
        rollback_complete = True
        for channel in self.permission_targets(guild):
            try:
                await self.restore_channel_restriction(channel, member, record)
            except (discord.Forbidden, discord.HTTPException) as error:
                rollback_complete = False
                log.error("Could not roll back jail permissions for member %s in channel %s: %s", member.id, channel.id, error)
        cfg = self.cfg(guild.id)
        role = guild.get_role(int(cfg.get("role_id", 0) or 0))
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Roll back an incomplete jail action")
            except (discord.Forbidden, discord.HTTPException) as error:
                rollback_complete = False
                log.error("Could not remove Jail role from member %s during rollback: %s", member.id, error)
        return rollback_complete

    async def unjail_member(
        self,
        guild: discord.Guild,
        member_id: int,
        moderator: str = "Dashboard",
    ) -> None:
        cfg = self.cfg(guild.id)
        record = cfg["members"].get(str(member_id))
        if not isinstance(record, dict):
            raise JailActionError("That member is not currently jailed.", 404)
        member = await self.resolve_member(guild, member_id)
        role_id = cfg.get("role_id", 0)
        role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
        if role is None:
            raise JailActionError("The saved Jail role is missing. Recreate the role before releasing jailed members.", 409)

        restored: list[discord.abc.GuildChannel] = []
        try:
            for channel in self.permission_targets(guild):
                await self.restore_channel_restriction(channel, member, record)
                restored.append(channel)
            await member.remove_roles(role, reason=f"Released from jail by {moderator}")
        except discord.Forbidden as error:
            await self.reapply_jail(guild, member, record, restored)
            raise JailActionError("KRYO cannot restore this member's channel permissions. Check Manage Roles.", 403) from error
        except discord.HTTPException as error:
            await self.reapply_jail(guild, member, record, restored)
            log.warning("Releasing member %s from jail failed in guild %s: %s", member.id, guild.id, error)
            raise JailActionError("Discord could not restore this member's permissions.", 502) from error

        cfg["members"].pop(str(member_id), None)
        self.save()

    async def send_command_response(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(message, ephemeral=ctx.interaction is not None)

    async def run_member_command(
        self,
        ctx: commands.Context,
        member: discord.Member,
        action: str,
        reason: str = "",
    ) -> None:
        guild = ctx.guild
        actor = ctx.author
        if guild is None or not isinstance(actor, discord.Member):
            await self.send_command_response(ctx, "This command can only be used by a server member.")
            return
        if (
            actor.id != guild.owner_id
            and not actor.guild_permissions.administrator
            and not actor.guild_permissions.manage_roles
        ):
            await self.send_command_response(ctx, "You need Manage Roles permission to use this command.")
            return
        if actor.id != guild.owner_id and member.top_role >= actor.top_role:
            await self.send_command_response(ctx, "You cannot use this command on a member with an equal or higher role.")
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        try:
            if action == "jail":
                await self.jail_member(guild, member.id, reason, str(actor))
                await self.send_command_response(
                    ctx,
                    f"🔒 {member.mention} was jailed. Their channel visibility is unchanged, but messaging and voice are disabled."
                    + (f"\nReason: {reason}" if reason else ""),
                )
            else:
                await self.unjail_member(guild, member.id, str(actor))
                await self.send_command_response(ctx, f"🔓 {member.mention} was released from jail.")
        except JailActionError as error:
            await self.send_command_response(ctx, str(error))

    @commands.hybrid_command(
        name="jail",
        description="Restrict a member from messaging and voice",
        brief="Restrict a member from messaging and voice.",
        extras={"category": "Moderation", "usage": ".jail <member> [reason]"},
    )
    @commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(member="Member to jail", reason="Why this member is being jailed")
    async def jail_command(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "",
    ) -> None:
        await self.run_member_command(ctx, member, "jail", reason.strip())

    @commands.hybrid_command(
        name="unjail",
        aliases=["release"],
        description="Restore a jailed member's permissions",
        brief="Release a member from jail.",
        extras={"category": "Moderation", "usage": ".unjail <member>"},
    )
    @commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(member="Member to release from jail")
    async def unjail_command(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        await self.run_member_command(ctx, member, "unjail")

    async def reapply_jail(
        self,
        guild: discord.Guild,
        member: discord.Member,
        record: dict[str, Any],
        channels: list[discord.abc.GuildChannel],
    ) -> None:
        for channel in channels:
            try:
                await self.apply_channel_restriction(channel, member, record)
            except (discord.Forbidden, discord.HTTPException) as error:
                log.error("Could not restore jail restrictions for member %s in channel %s: %s", member.id, channel.id, error)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if channel not in self.permission_targets(channel.guild):
            return
        cfg = self.cfg(channel.guild.id)
        for member_id, record in cfg["members"].items():
            if not isinstance(record, dict):
                continue
            try:
                member = await self.resolve_member(channel.guild, int(member_id))
                await self.apply_channel_restriction(channel, member, record)
                self.save()
            except (ValueError, JailActionError, discord.Forbidden, discord.HTTPException) as error:
                log.error("Could not apply jail restrictions in new channel %s for member %s: %s", channel.id, member_id, error)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.cfg(member.guild.id)
        record = cfg["members"].get(str(member.id))
        if not isinstance(record, dict):
            return
        role_id = cfg.get("role_id", 0)
        role = member.guild.get_role(int(role_id)) if str(role_id).isdigit() else None
        if role is None:
            log.error("Jail role is missing in guild %s; returning jailed member %s could not be assigned the role.", member.guild.id, member.id)
            return
        try:
            for channel in self.permission_targets(member.guild):
                await self.apply_channel_restriction(channel, member, record)
            if role not in member.roles:
                await member.add_roles(role, reason="Restore KRYO jail status for a returning member")
            self.save()
        except (discord.Forbidden, discord.HTTPException) as error:
            log.error("Could not restore jail status for returning member %s in guild %s: %s", member.id, member.guild.id, error)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready:
            return
        self._ready = True
        for guild in self.bot.guilds:
            cfg = self.cfg(guild.id)
            role = guild.get_role(int(cfg["role_id"])) if str(cfg["role_id"]).isdigit() else None
            for member_id, record in cfg["members"].items():
                if not isinstance(record, dict):
                    continue
                try:
                    member = await self.resolve_member(guild, int(member_id))
                    if role is None:
                        log.error("Jail role is missing in guild %s; jailed member %s could not be restored.", guild.id, member_id)
                        continue
                    for channel in self.permission_targets(guild):
                        await self.apply_channel_restriction(channel, member, record)
                    if role not in member.roles:
                        await member.add_roles(role, reason="Restore saved KRYO jail status")
                    voice_state = getattr(member, "voice", None)
                    if voice_state is not None and voice_state.channel is not None:
                        if guild.me is not None and guild.me.guild_permissions.move_members:
                            await member.move_to(None, reason="Disconnect a jailed member from voice")
                        else:
                            log.error("KRYO needs Move Members to disconnect jailed member %s from voice in guild %s.", member.id, guild.id)
                    self.save()
                except (ValueError, JailActionError, discord.Forbidden, discord.HTTPException) as error:
                    log.error("Could not restore jail status for member %s in guild %s: %s", member_id, guild.id, error)
