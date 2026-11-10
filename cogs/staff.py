import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui
from botcore.perms import admin_prefix, admin_slash, member_problem, role_problem
from botcore.moderation_embeds import (
    build_ban_embed,
    build_moderation_action_dm,
    build_unban_embed,
    build_warning_dm,
    moderation_emojis_for,
)

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(text: str) -> Optional[discord.Colour]:
    match = HEX_RE.match(text.strip())
    return discord.Colour(int(match.group(1), 16)) if match else None


def action_embed(
    guild: discord.Guild,
    past: str,
    reason: str,
    moderator: Optional[discord.abc.User] = None,
    emojis: Optional[dict[str, str]] = None,
    bot_avatar_url: Optional[str] = None,
) -> discord.Embed:
    emb = build_moderation_action_dm(
        guild_name=guild.name,
        action=past,
        reason=reason,
        moderator=str(moderator) if moderator is not None else None,
        emojis=emojis or {},
        bot_avatar_url=bot_avatar_url,
    )
    if guild.icon is not None:
        emb.set_thumbnail(url=guild.icon.url)
    return emb


async def try_dm(user: discord.abc.User, emb: discord.Embed) -> bool:
    try:
        await user.send(embed=emb)
        return True
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Moderation DM could not be sent to {user.id}: {type(error).__name__}: {error}")
        return False


def need_perm(name: str) -> discord.Embed:
    return ui.error("Missing Permissions", f"I need the **{name}** permission (and a high enough role) to do that.")


MODERATION_AUDIT_ACTIONS = frozenset(
    {
        discord.AuditLogAction.ban,
        discord.AuditLogAction.unban,
        discord.AuditLogAction.kick,
        discord.AuditLogAction.member_update,
        discord.AuditLogAction.member_role_update,
    }
)


async def recent_moderation_entries(
    guild: discord.Guild,
    member_id: int,
    limit: int,
) -> list[discord.AuditLogEntry]:
    entries = []
    async for entry in guild.audit_logs(limit=100):
        target = entry.target
        if entry.action in MODERATION_AUDIT_ACTIONS and target is not None and target.id == member_id:
            entries.append(entry)
            if len(entries) >= limit:
                break
    return entries


def moderation_history_embed(
    member: discord.Member,
    entries: list[discord.AuditLogEntry],
) -> discord.Embed:
    if not entries:
        return ui.info("No Recent Moderation", f"No recent moderation actions were found for {member.mention}.")
    lines = []
    for entry in entries:
        action = entry.action.name.replace("_", " ").title()
        moderator = entry.user.mention if entry.user is not None else "Unknown moderator"
        reason = discord.utils.escape_mentions(
            discord.utils.escape_markdown(entry.reason or "No reason recorded")
        ).replace("\n", " ")[:160]
        lines.append(
            f"**{action}** by {moderator} · {discord.utils.format_dt(entry.created_at, 'R')}\n> {reason}"
        )
    return ui.panel(f"Recent Moderation · {member.display_name}", None, lines[:10])


def make_purge_check(
    limit: int,
    predicate: Callable[[discord.Message], bool],
    excluded_message_id: int,
) -> Callable[[discord.Message], bool]:
    matched = 0

    def check(message: discord.Message) -> bool:
        nonlocal matched
        if message.id == excluded_message_id or matched >= limit or not predicate(message):
            return False
        matched += 1
        return True

    return check


# ======================================================================= ROLE CONFIG
class RoleConfigCog(commands.Cog, name="Role Config"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def do_add(self, guild, actor, member: discord.Member, role: discord.Role) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        if role in member.roles:
            return ui.warn("Already Has Role", f"{member.mention} already has {role.mention}.")
        try:
            await member.add_roles(role, reason=f"Role added by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Added", f"{role.mention} was given to {member.mention}.")

    async def do_remove(self, guild, actor, member: discord.Member, role: discord.Role) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        if role not in member.roles:
            return ui.warn("Role Not Found", f"{member.mention} does not have {role.mention}.")
        try:
            await member.remove_roles(role, reason=f"Role removed by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Removed", f"{role.mention} was removed from {member.mention}.")

    async def do_create(self, guild, actor, name: str, color: Optional[str]) -> discord.Embed:
        colour = discord.Colour.default()
        if color:
            parsed = parse_color(color)
            if parsed is None:
                return ui.warn("Invalid Color", "Use a hex color like `#FF0000`.")
            colour = parsed
        try:
            role = await guild.create_role(name=name[:100], colour=colour, reason=f"Role created by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Created", f"{role.mention} was created.")

    async def do_delete(self, guild, actor, role: discord.Role) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        name = role.name
        try:
            await role.delete(reason=f"Role deleted by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Deleted", f"The role **{name}** was deleted.")

    async def do_color(self, guild, actor, role: discord.Role, color: str) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        parsed = parse_color(color)
        if parsed is None:
            return ui.warn("Invalid Color", "Use a hex color like `#FF0000`.")
        try:
            await role.edit(colour=parsed, reason=f"Color changed by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Color Updated", f"{role.mention} now uses `#{parsed.value:06X}`.")

    async def do_rename(self, guild, actor, role: discord.Role, name: str) -> discord.Embed:
        problem = role_problem(guild, actor, role)
        if problem:
            return ui.error("Action Blocked", problem)
        try:
            await role.edit(name=name[:100], reason=f"Renamed by {actor}")
        except discord.Forbidden:
            return need_perm("Manage Roles")
        return ui.success("Role Renamed", f"The role is now called {role.mention}.")

    def do_info(self, role: discord.Role) -> discord.Embed:
        rows = [
            ("Role", role.mention),
            ("ID", f"`{role.id}`"),
            ("Color", f"`#{role.colour.value:06X}`"),
            ("Members", f"`{len(role.members)}`"),
            ("Position", f"`{role.position}`"),
            ("Mentionable", "`yes`" if role.mentionable else "`no`"),
            ("Shown separately", "`yes`" if role.hoist else "`no`"),
            ("Created", discord.utils.format_dt(role.created_at, "R")),
        ]
        return ui.panel("Role Information", None, rows)

    def do_list(self, guild: discord.Guild) -> discord.Embed:
        roles = [role for role in reversed(guild.roles) if not role.is_default()][:40]
        text = "\n".join(f"{ui.SPIDER} {role.mention} : `{len(role.members)}` members" for role in roles) or "No roles."
        return ui.info("Server Roles", text[:4000])

    # ------------------------------------------------------------------ dot commands
    @commands.group(name="role", invoke_without_command=True, brief="Manage roles: add, remove, create, delete, info, color, rename, list.",
                    extras={"category": "Role Config", "usage": ".role add|remove|create|delete|info|color|rename|list"})
    @commands.guild_only()
    @admin_prefix
    async def role_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=ui.info("Role Config", "Use `.role add @user @role`, `.role remove`, `.role create <name> [#hex]`, `.role delete`, `.role info`, `.role color`, `.role rename`, `.role list`."))

    @role_prefix.command(name="add", brief="Give a role: .role add @user @role")
    @commands.guild_only()
    @admin_prefix
    async def role_add(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        await ctx.send(embed=await self.do_add(ctx.guild, ctx.author, member, role))

    @role_prefix.command(name="remove", brief="Remove a role: .role remove @user @role")
    @commands.guild_only()
    @admin_prefix
    async def role_remove(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        await ctx.send(embed=await self.do_remove(ctx.guild, ctx.author, member, role))

    @role_prefix.command(name="create", brief="Create a role: .role create <name> [#hex]")
    @commands.guild_only()
    @admin_prefix
    async def role_create(self, ctx: commands.Context, name: str, color: Optional[str] = None) -> None:
        await ctx.send(embed=await self.do_create(ctx.guild, ctx.author, name, color))

    @role_prefix.command(name="delete", brief="Delete a role: .role delete @role")
    @commands.guild_only()
    @admin_prefix
    async def role_delete(self, ctx: commands.Context, role: discord.Role) -> None:
        await ctx.send(embed=await self.do_delete(ctx.guild, ctx.author, role))

    @role_prefix.command(name="color", brief="Change a role color: .role color @role #hex")
    @commands.guild_only()
    @admin_prefix
    async def role_color(self, ctx: commands.Context, role: discord.Role, color: str) -> None:
        await ctx.send(embed=await self.do_color(ctx.guild, ctx.author, role, color))

    @role_prefix.command(name="rename", brief="Rename a role: .role rename @role <name>")
    @commands.guild_only()
    @admin_prefix
    async def role_rename(self, ctx: commands.Context, role: discord.Role, *, name: str) -> None:
        await ctx.send(embed=await self.do_rename(ctx.guild, ctx.author, role, name))

    @role_prefix.command(name="info", brief="Show role details: .role info @role")
    @commands.guild_only()
    @admin_prefix
    async def role_info(self, ctx: commands.Context, role: discord.Role) -> None:
        await ctx.send(embed=self.do_info(role))

    @role_prefix.command(name="list", brief="List every role.")
    @commands.guild_only()
    @admin_prefix
    async def role_list(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.do_list(ctx.guild))

    # ---------------------------------------------------------------- slash commands
    role = app_commands.Group(name="role", description="Role configuration", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @role.command(name="add", description="Give a role to a member")
    @admin_slash
    async def s_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        await ui.respond(interaction, await self.do_add(interaction.guild, interaction.user, member, role))

    @role.command(name="remove", description="Remove a role from a member")
    @admin_slash
    async def s_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        await ui.respond(interaction, await self.do_remove(interaction.guild, interaction.user, member, role))

    @role.command(name="create", description="Create a new role")
    @admin_slash
    async def s_create(self, interaction: discord.Interaction, name: str, color: Optional[str] = None) -> None:
        await ui.respond(interaction, await self.do_create(interaction.guild, interaction.user, name, color))

    @role.command(name="delete", description="Delete a role")
    @admin_slash
    async def s_delete(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await ui.respond(interaction, await self.do_delete(interaction.guild, interaction.user, role))

    @role.command(name="info", description="Show role details")
    @admin_slash
    async def s_info(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await ui.respond(interaction, self.do_info(role))


# ======================================================================= MODERATION (dot)
class ModerationCog(commands.Cog, name="Moderation"):
    """Dot-command versions of the moderation tools (the slash versions live in main.py)."""

    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def _moderation_emojis(self, guild_id: int) -> dict[str, str]:
        mass_cog = self.bot.get_cog("MassMentionCog")
        return dict(getattr(mass_cog, "emojis", {}).get(guild_id, {}))

    async def _moderation_username(self, user_id: int) -> str:
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.HTTPException as error:
            print(f"Could not fetch user {user_id} for moderation embed: {type(error).__name__}: {error}")
            return str(user_id)
        return user.name

    async def _send_moderation_embed(self, ctx: commands.Context, embed: discord.Embed) -> None:
        try:
            await ctx.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Moderation result embed could not be sent: {type(error).__name__}: {error}")

    async def _respond_moderation_embed(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        ephemeral: bool = False,
    ) -> None:
        try:
            await ui.respond(interaction, embed, ephemeral=ephemeral)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Moderation slash response could not be sent: {type(error).__name__}: {error}")

    async def do_filtered_purge(
        self,
        ctx: commands.Context,
        amount: int,
        predicate: Callable[[discord.Message], bool],
        label: str,
    ) -> None:
        if not 1 <= amount <= 100:
            await ctx.send(embed=ui.warn("Invalid Amount", "Choose an amount from 1 to 100."))
            return
        if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            await ctx.send(embed=ui.warn("Unsupported Channel", "Use this command in a text or thread channel."))
            return
        check = make_purge_check(amount, predicate, ctx.message.id)
        try:
            deleted = await ctx.channel.purge(
                limit=1000,
                check=check,
                reason=f"{label} cleanup by {ctx.author}",
            )
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Messages"))
            return
        except discord.HTTPException as error:
            await ctx.send(embed=ui.error("Purge Failed", f"Discord could not delete those messages: {error}"))
            return
        await ctx.send(
            embed=ui.success("Messages Deleted", f"Deleted **{len(deleted)}** {label} message(s)."),
            delete_after=5,
        )

    @commands.command(
        name="modhistory",
        brief="Show recent moderation actions for a member.",
        extras={"category": "Moderation", "usage": ".modhistory @member [1-10]"},
    )
    @commands.guild_only()
    @commands.has_guild_permissions(view_audit_log=True)
    @admin_prefix
    async def modhistory(
        self,
        ctx: commands.Context,
        member: discord.Member,
        limit: int = 10,
    ) -> None:
        if not 1 <= limit <= 10:
            await ctx.send(embed=ui.warn("Invalid Amount", "Choose a history size from 1 to 10."))
            return
        if ctx.guild is None:
            await ctx.send(embed=ui.warn("Server Only", "Use this command inside a server."))
            return
        try:
            entries = await recent_moderation_entries(ctx.guild, member.id, limit)
        except discord.Forbidden:
            await ctx.send(embed=need_perm("View Audit Log"))
            return
        except discord.HTTPException as error:
            await ctx.send(embed=ui.error("History Unavailable", f"Discord could not load the audit log: {error}"))
            return
        await ctx.send(
            embed=moderation_history_embed(member, entries),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="purgeuser",
        brief="Delete recent messages from one member (1-100).",
        extras={"category": "Moderation", "usage": ".purgeuser @member [amount]"},
    )
    @commands.guild_only()
    @admin_prefix
    async def purgeuser(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int = 25,
    ) -> None:
        await self.do_filtered_purge(
            ctx,
            amount,
            lambda message: message.author.id == member.id,
            f"from {member.display_name}",
        )

    @commands.command(
        name="purgebots",
        brief="Delete recent bot messages (1-100).",
        extras={"category": "Moderation", "usage": ".purgebots [amount]"},
    )
    @commands.guild_only()
    @admin_prefix
    async def purgebots(self, ctx: commands.Context, amount: int = 25) -> None:
        await self.do_filtered_purge(
            ctx,
            amount,
            lambda message: message.author.bot,
            "from bots",
        )

    @commands.command(name="ban", brief="Ban a member or user ID.", extras={"category": "Moderation", "usage": ".ban @user [reason]"})
    @commands.guild_only()
    @admin_prefix
    async def ban(self, ctx: commands.Context, user: Union[discord.Member, discord.User], *, reason: str = "No reason given") -> None:
        if isinstance(user, discord.Member):
            problem = member_problem(ctx.guild, ctx.author, user)
            if problem:
                await ctx.send(embed=ui.error("Action Blocked", problem))
                return
            await try_dm(
                user,
                action_embed(
                    ctx.guild, "banned", reason, ctx.author,
                    moderation_emojis_for(self.bot, ctx.guild.id),
                    self.bot.user.display_avatar.url if self.bot.user else None,
                ),
            )
        try:
            await ctx.guild.ban(user, reason=f"{reason} | by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Ban Members"))
            return
        username = await self._moderation_username(user.id)
        await self._send_moderation_embed(
            ctx,
            build_ban_embed(
                username=username,
                reason=reason,
                emojis=self._moderation_emojis(ctx.guild.id),
                bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )

    @commands.command(name="unban", brief="Unban a user by ID.", extras={"category": "Moderation", "usage": ".unban <user id>"})
    @commands.guild_only()
    @admin_prefix
    async def unban(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason given") -> None:
        try:
            await ctx.guild.unban(user, reason=f"{reason} | by {ctx.author}")
        except discord.NotFound:
            await ctx.send(embed=ui.warn("Not Banned", "This user is not banned."))
            return
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Ban Members"))
            return
        await try_dm(
            user,
            action_embed(
                ctx.guild, "unbanned", reason, ctx.author,
                moderation_emojis_for(self.bot, ctx.guild.id),
                self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )
        username = await self._moderation_username(user.id)
        await self._send_moderation_embed(
            ctx,
            build_unban_embed(
                username=username,
                emojis=self._moderation_emojis(ctx.guild.id),
                bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )

    @app_commands.command(name="ban", description="Ban a member from this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="Member to ban", reason="Reason for the ban")
    @admin_slash
    async def ban_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason given",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond_moderation_embed(
                interaction,
                ui.warn("Server Only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return
        problem = member_problem(guild, interaction.user, member)
        if problem:
            await self._respond_moderation_embed(
                interaction,
                ui.error("Action Blocked", problem),
                ephemeral=True,
            )
            return

        await try_dm(
            member,
            action_embed(
                guild, "banned", reason, interaction.user,
                moderation_emojis_for(self.bot, guild.id),
                self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )
        try:
            await guild.ban(member, reason=f"{reason} | by {interaction.user}")
        except discord.Forbidden:
            await self._respond_moderation_embed(
                interaction,
                need_perm("Ban Members"),
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            print(f"Slash ban failed for {member.id}: {type(error).__name__}: {error}")
            await self._respond_moderation_embed(
                interaction,
                ui.error("Ban Failed", "Discord could not complete the ban. Please try again."),
                ephemeral=True,
            )
            return

        username = await self._moderation_username(member.id)
        await self._respond_moderation_embed(
            interaction,
            build_ban_embed(
                username=username,
                reason=reason,
                emojis=self._moderation_emojis(guild.id),
                bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )

    @app_commands.command(name="unban", description="Unban a user by their Discord ID.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason for the unban")
    @admin_slash
    async def unban_slash(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason given",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond_moderation_embed(
                interaction,
                ui.warn("Server Only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return
        if not user_id.isascii() or not user_id.isdigit() or int(user_id) <= 0:
            await self._respond_moderation_embed(
                interaction,
                ui.warn("Invalid User ID", "Enter the numeric Discord user ID to unban."),
                ephemeral=True,
            )
            return
        target_id = int(user_id)
        try:
            user = await self.bot.fetch_user(target_id)
        except discord.HTTPException as error:
            print(f"Could not fetch user {target_id} for unban DM: {type(error).__name__}: {error}")
            user = None
        try:
            await guild.unban(
                discord.Object(id=target_id),
                reason=f"{reason} | by {interaction.user}",
            )
        except discord.NotFound:
            await self._respond_moderation_embed(
                interaction,
                ui.warn("Not Banned", "This user is not banned."),
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await self._respond_moderation_embed(
                interaction,
                need_perm("Ban Members"),
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            print(f"Slash unban failed for {target_id}: {type(error).__name__}: {error}")
            await self._respond_moderation_embed(
                interaction,
                ui.error("Unban Failed", "Discord could not complete the unban. Please try again."),
                ephemeral=True,
            )
            return

        if user is not None:
            await try_dm(
                user,
                action_embed(
                    guild, "unbanned", reason, interaction.user,
                    moderation_emojis_for(self.bot, guild.id),
                    self.bot.user.display_avatar.url if self.bot.user else None,
                ),
            )
        username = await self._moderation_username(target_id)
        await self._respond_moderation_embed(
            interaction,
            build_unban_embed(
                username=username,
                emojis=self._moderation_emojis(guild.id),
                bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )

    @commands.command(name="kick", brief="Kick a member.", extras={"category": "Moderation", "usage": ".kick @user [reason]"})
    @commands.guild_only()
    @admin_prefix
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
        problem = member_problem(ctx.guild, ctx.author, member)
        if problem:
            await ctx.send(embed=ui.error("Action Blocked", problem))
            return
        await try_dm(
            member,
            action_embed(
                ctx.guild, "kicked", reason, ctx.author,
                moderation_emojis_for(self.bot, ctx.guild.id),
                self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )
        try:
            await ctx.guild.kick(member, reason=f"{reason} | by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Kick Members"))
            return
        await ctx.send(embed=ui.success("Member Kicked", f"**{member}** was kicked.\n**Reason:** {reason}"))

    @commands.command(name="mute", aliases=["timeout"], brief="Timeout a member (minutes).", extras={"category": "Moderation", "usage": ".mute @user [minutes] [reason]"})
    @commands.guild_only()
    @admin_prefix
    async def mute(self, ctx: commands.Context, member: discord.Member, minutes: int = 60, *, reason: str = "No reason given") -> None:
        problem = member_problem(ctx.guild, ctx.author, member)
        if problem:
            await ctx.send(embed=ui.error("Action Blocked", problem))
            return
        if member.guild_permissions.administrator:
            await ctx.send(embed=ui.error("Action Blocked", "Discord does not allow timing out administrators."))
            return
        minutes = max(1, min(minutes, 40320))
        await try_dm(
            member,
            action_embed(
                ctx.guild, "timed out", f"{reason} ({minutes} minutes)", ctx.author,
                moderation_emojis_for(self.bot, ctx.guild.id),
                self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"{reason} | by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Moderate Members"))
            return
        await ctx.send(embed=ui.success("Member Timed Out", f"{member.mention} was timed out for **{minutes}** minute(s).\n**Reason:** {reason}"))

    @commands.command(name="unmute", aliases=["untimeout"], brief="Remove a timeout.", extras={"category": "Moderation", "usage": ".unmute @user"})
    @commands.guild_only()
    @admin_prefix
    async def unmute(self, ctx: commands.Context, member: discord.Member) -> None:
        problem = member_problem(ctx.guild, ctx.author, member)
        if problem:
            await ctx.send(embed=ui.error("Action Blocked", problem))
            return
        try:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Moderate Members"))
            return
        await ctx.send(embed=ui.success("Timeout Removed", f"The timeout on {member.mention} was removed."))

    @commands.command(name="warn", brief="Warn a member (Smart Moderation can punish automatically).", extras={"category": "Moderation", "usage": ".warn @user <reason>"})
    @commands.guild_only()
    @admin_prefix
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str) -> None:
        problem = member_problem(ctx.guild, ctx.author, member)
        if problem:
            await ctx.send(embed=ui.error("Action Blocked", problem))
            return
        warnings = self.state["warnings"].setdefault(f"{ctx.guild.id}:{member.id}", [])
        warnings.append({"reason": reason, "moderator_id": ctx.author.id, "created_at": datetime.now(timezone.utc).isoformat()})
        self.save()
        await try_dm(
            member,
            build_warning_dm(
                guild_name=ctx.guild.name,
                reason=reason,
                total_warnings=len(warnings),
                emojis=moderation_emojis_for(self.bot, ctx.guild.id),
                bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            ),
        )
        desc = f"{member.mention} received a warning.\n**Total warnings:** {len(warnings)}\n**Reason:** {reason}"
        smart = self.bot.get_cog("Smart Moderation")
        note = await smart.apply(ctx.guild, member, len(warnings)) if smart is not None else None
        if note:
            desc += f"\n\n> **{note}**"
        await ctx.send(embed=ui.warn("Member Warned", desc))

    @commands.command(name="warnings", aliases=["warns"], brief="Show a member's warnings.", extras={"category": "Moderation", "usage": ".warnings @user"})
    @commands.guild_only()
    @admin_prefix
    async def warnings(self, ctx: commands.Context, member: discord.Member) -> None:
        items = self.state["warnings"].get(f"{ctx.guild.id}:{member.id}", [])
        if not items:
            await ctx.send(embed=ui.info("No Warnings", "This member has no warnings."))
            return
        start = max(1, len(items) - 9)
        lines = [f"`{index}.` {item['reason']} · <@{item['moderator_id']}>" for index, item in enumerate(items[-10:], start=start)]
        await ctx.send(embed=ui.info(f"Warnings for {member.display_name}", "\n".join(lines)[:4000]))

    @commands.command(name="clearwarns", aliases=["clearwarnings"], brief="Clear all warnings of a member.", extras={"category": "Moderation", "usage": ".clearwarns @user"})
    @commands.guild_only()
    @admin_prefix
    async def clearwarns(self, ctx: commands.Context, member: discord.Member) -> None:
        removed = self.state["warnings"].pop(f"{ctx.guild.id}:{member.id}", None)
        if not removed:
            await ctx.send(embed=ui.info("No Warnings", "This member has no warnings."))
            return
        self.save()
        await ctx.send(embed=ui.success("Warnings Cleared", f"All warnings for {member.mention} were cleared."))

    @commands.command(name="purge", aliases=["clear"], brief="Delete recent messages (1-100).", extras={"category": "Moderation", "usage": ".purge <amount>"})
    @commands.guild_only()
    @admin_prefix
    async def purge(self, ctx: commands.Context, amount: int) -> None:
        amount = max(1, min(amount, 100))
        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Messages"))
            return
        await ctx.send(embed=ui.success("Messages Deleted", f"**{max(0, len(deleted) - 1)}** messages were deleted."), delete_after=5)

    @commands.command(name="slowmode", aliases=["sm"], brief="Set channel slowmode in seconds (0 = off).", extras={"category": "Moderation", "usage": ".slowmode <seconds> [#channel]"})
    @commands.guild_only()
    @admin_prefix
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        seconds = max(0, min(seconds, 21600))
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Channels"))
            return
        await ctx.send(embed=ui.success("Slowmode Updated", f"Slowmode in {channel.mention} is now **{seconds}s**."))

    @commands.command(name="lock", brief="Stop @everyone from sending messages in a channel.", extras={"category": "Moderation", "usage": ".lock [#channel]"})
    @commands.guild_only()
    @admin_prefix
    async def lock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        try:
            await channel.set_permissions(ctx.guild.default_role, send_messages=False, reason=f"Locked by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Channels"))
            return
        await ctx.send(embed=ui.success("Channel Locked", f"{channel.mention} is now locked."))

    @commands.command(name="unlock", brief="Let @everyone send messages again.", extras={"category": "Moderation", "usage": ".unlock [#channel]"})
    @commands.guild_only()
    @admin_prefix
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        try:
            await channel.set_permissions(ctx.guild.default_role, send_messages=None, reason=f"Unlocked by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Channels"))
            return
        await ctx.send(embed=ui.success("Channel Unlocked", f"{channel.mention} is now unlocked."))

    @commands.command(name="nick", brief="Change or reset a member nickname.", extras={"category": "Moderation", "usage": ".nick @user [new nickname]"})
    @commands.guild_only()
    @admin_prefix
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: Optional[str] = None) -> None:
        problem = member_problem(ctx.guild, ctx.author, member)
        if problem:
            await ctx.send(embed=ui.error("Action Blocked", problem))
            return
        try:
            await member.edit(nick=nickname[:32] if nickname else None, reason=f"Nickname changed by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Nicknames"))
            return
        await ctx.send(embed=ui.success("Nickname Updated", f"{member.mention} is now **{nickname[:32] if nickname else 'reset'}**."))


# ======================================================================= SMART MODERATION
DEFAULT_RULES = {"3": {"action": "timeout", "minutes": 60}, "5": {"action": "kick"}, "7": {"action": "ban"}}
SMART_ACTIONS = ("timeout", "kick", "ban")


class SmartModCog(commands.Cog, name="Smart Moderation"):
    """Escalating punishments: the more warnings a member collects, the harder the automatic action."""

    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def cfg(self, guild_id: int) -> dict:
        settings = self.state["settings"].setdefault(str(guild_id), {})
        smart = settings.setdefault("smartmod", {})
        smart.setdefault("enabled", False)
        if "rules" not in smart:
            smart["rules"] = {key: dict(value) for key, value in DEFAULT_RULES.items()}
        return smart

    def feature_on(self, guild: discord.Guild) -> bool:
        return bool(self.cfg(guild.id)["enabled"])

    # ------------------------------------------------------- behaviour scoring
    def risk_score(self, guild: discord.Guild, member: discord.Member, count: int) -> float:
        """Behaviour risk for one member, used to make punishments smarter.

        The score blends three things the warning count cannot see on its own:
        how fast the warnings arrived, whether the account is new, and whether
        the security layer has already flagged this member. A member who collects
        five warnings over months is treated differently from one who collects
        five in a minute next to a detected raid, while the configured rule
        count still decides the actual punishment.
        """
        score = float(count) * 8.0
        recent = self.state["warnings"].get(f"{guild.id}:{member.id}", [])
        moments = []
        for item in recent[-12:]:
            stamp = item.get("at") if isinstance(item, dict) else None
            if isinstance(stamp, (int, float)):
                moments.append(float(stamp))
        if len(moments) >= 3:
            now = time.time()
            window = [moment for moment in moments if now - moment <= 600]
            if len(window) >= 3:
                score += min(30.0, (len(window) - 2) * 6.0)
        age_days = (discord.utils.utcnow() - member.created_at).days
        if age_days < 2:
            score += 18.0
        elif age_days < 7:
            score += 8.0
        security = self.bot.get_cog("Security")
        if security is not None and getattr(security, "risk", None) is not None:
            score += min(60.0, security.risk.score_of(guild.id, member.id))
        # Newer accounts are riskier, but never punish on the score alone.
        return min(100.0, score)

    def severity_band(self, score: float) -> str:
        if score >= 70:
            return "HIGH"
        if score >= 35:
            return "MEDIUM"
        return "LOW"

    async def apply(self, guild: discord.Guild, member: discord.Member, count: int) -> Optional[str]:
        """Called after a warning. Returns a short sentence describing what happened (or None)."""
        smart = self.cfg(guild.id)
        if not smart["enabled"]:
            return None
        rule = smart["rules"].get(str(count))
        if rule is None:
            return None
        me = guild.me
        if member.id == guild.owner_id or (self.bot.user is not None and member.id == self.bot.user.id) or me is None or member.top_role >= me.top_role:
            return f"Smart Moderation reached {count} warnings but could not act (role too high)."
        action = rule["action"]
        score = self.risk_score(guild, member, count)
        severity = self.severity_band(score)
        reason = f"Smart Moderation: reached {count} warnings (risk {score:.0f}/100, {severity})"
        try:
            if action == "timeout":
                if member.guild_permissions.administrator:
                    return f"Smart Moderation reached {count} warnings but administrators cannot be timed out."
                minutes = int(rule.get("minutes", 60))
                await try_dm(
                    member,
                    action_embed(
                        guild, "timed out", f"{reason} ({minutes} minutes)",
                        emojis=moderation_emojis_for(self.bot, guild.id),
                        bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                    ),
                )
                await member.timeout(timedelta(minutes=minutes), reason=reason)
                return f"Smart Moderation: timed out for {minutes} minutes ({count} warnings, risk {score:.0f})."
            if action == "kick":
                await try_dm(
                    member,
                    action_embed(
                        guild, "kicked", reason,
                        emojis=moderation_emojis_for(self.bot, guild.id),
                        bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                    ),
                )
                await guild.kick(member, reason=reason)
                return f"Smart Moderation: member kicked ({count} warnings, risk {score:.0f})."
            await try_dm(
                member,
                action_embed(
                    guild, "banned", reason,
                    emojis=moderation_emojis_for(self.bot, guild.id),
                    bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                ),
            )
            await guild.ban(member, reason=reason)
            return f"Smart Moderation: member banned ({count} warnings, risk {score:.0f})."
        except discord.Forbidden:
            return f"Smart Moderation reached {count} warnings but I am missing permissions."
        except discord.HTTPException:
            return f"Smart Moderation reached {count} warnings but Discord refused the action."

    # ---- shared embeds
    def status_embed(self, guild: discord.Guild) -> discord.Embed:
        smart = self.cfg(guild.id)
        rules = sorted(smart["rules"].items(), key=lambda item: int(item[0]))
        rows = []
        for count, rule in rules:
            extra = f" · {rule.get('minutes', 60)} min" if rule["action"] == "timeout" else ""
            rows.append((f"{count} warnings", f"`{rule['action'].title()}{extra}`"))
        return ui.panel(
            "Smart Moderation",
            f"Status: **{'ON' if smart['enabled'] else 'OFF'}**\nWarnings given with `.warn` or `/moderation warn` trigger these automatic punishments.",
            rows or [("No rules", None)],
            notes=["Use .smartmod set <warnings> <timeout|kick|ban> to add a rule"],
        )

    def toggle(self, guild: discord.Guild, on: bool) -> discord.Embed:
        self.cfg(guild.id)["enabled"] = on
        self.save()
        if on:
            return ui.security("Smart Moderation Enabled", "Warnings now escalate automatically. Run `.smartmod` to see the rules.")
        return ui.warn("Smart Moderation Disabled", "Warnings will no longer trigger automatic punishments.")

    def set_rule(self, guild: discord.Guild, warns: int, action: str, minutes: int = 60) -> discord.Embed:
        if not 1 <= warns <= 50 or action not in SMART_ACTIONS:
            return ui.warn("Invalid Rule", "Warnings must be 1-50 and the action must be `timeout`, `kick` or `ban`.")
        rule: dict[str, Any] = {"action": action}
        if action == "timeout":
            rule["minutes"] = max(1, min(minutes, 40320))
        self.cfg(guild.id)["rules"][str(warns)] = rule
        self.save()
        return ui.success("Rule Saved", f"At **{warns}** warnings the member will receive: **{action}**.")

    def remove_rule(self, guild: discord.Guild, warns: int) -> discord.Embed:
        if self.cfg(guild.id)["rules"].pop(str(warns), None) is None:
            return ui.warn("Rule Not Found", f"There is no rule for **{warns}** warnings.")
        self.save()
        return ui.success("Rule Removed", f"The rule for **{warns}** warnings was removed.")

    def reset(self, guild: discord.Guild) -> discord.Embed:
        self.cfg(guild.id)["rules"] = {key: dict(value) for key, value in DEFAULT_RULES.items()}
        self.save()
        return ui.success("Rules Reset", "The default rules were restored: 3 warnings timeout, 5 kick, 7 ban.")

    # ---- dot commands
    @commands.group(name="smartmod", invoke_without_command=True, brief="Automatic escalation for warnings.",
                    extras={"category": "Smart Moderation", "usage": ".smartmod on|off|set|remove|reset"})
    @commands.guild_only()
    @admin_prefix
    async def smartmod_prefix(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.status_embed(ctx.guild))

    @smartmod_prefix.command(name="on", brief="Turn Smart Moderation on.")
    @commands.guild_only()
    @admin_prefix
    async def sm_on(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.toggle(ctx.guild, True))

    @smartmod_prefix.command(name="off", brief="Turn Smart Moderation off.")
    @commands.guild_only()
    @admin_prefix
    async def sm_off(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.toggle(ctx.guild, False))

    @smartmod_prefix.command(name="set", brief="Add a rule: .smartmod set 3 timeout 60")
    @commands.guild_only()
    @admin_prefix
    async def sm_set(self, ctx: commands.Context, warnings: int, action: str, minutes: int = 60) -> None:
        await ctx.send(embed=self.set_rule(ctx.guild, warnings, action.lower(), minutes))

    @smartmod_prefix.command(name="remove", brief="Remove a rule: .smartmod remove 3")
    @commands.guild_only()
    @admin_prefix
    async def sm_remove(self, ctx: commands.Context, warnings: int) -> None:
        await ctx.send(embed=self.remove_rule(ctx.guild, warnings))

    @smartmod_prefix.command(name="reset", brief="Restore the default rules.")
    @commands.guild_only()
    @admin_prefix
    async def sm_reset(self, ctx: commands.Context) -> None:
        await ctx.send(embed=self.reset(ctx.guild))

    # ---- slash commands
    smartmod = app_commands.Group(name="smartmod", description="Smart Moderation", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @smartmod.command(name="status", description="Show the Smart Moderation rules")
    @admin_slash
    async def s_status(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.status_embed(interaction.guild))

    @smartmod.command(name="toggle", description="Turn Smart Moderation on or off")
    @admin_slash
    async def s_toggle(self, interaction: discord.Interaction, state: Literal["on", "off"]) -> None:
        await ui.respond(interaction, self.toggle(interaction.guild, state == "on"))

    @smartmod.command(name="set", description="Add or change a rule")
    @app_commands.choices(action=[app_commands.Choice(name=name.title(), value=name) for name in SMART_ACTIONS])
    @admin_slash
    async def s_set(self, interaction: discord.Interaction, warnings: app_commands.Range[int, 1, 50], action: app_commands.Choice[str], minutes: app_commands.Range[int, 1, 40320] = 60) -> None:
        await ui.respond(interaction, self.set_rule(interaction.guild, warnings, action.value, minutes))

    @smartmod.command(name="remove", description="Remove a rule")
    @admin_slash
    async def s_remove(self, interaction: discord.Interaction, warnings: app_commands.Range[int, 1, 50]) -> None:
        await ui.respond(interaction, self.remove_rule(interaction.guild, warnings))

    @smartmod.command(name="reset", description="Restore the default rules")
    @admin_slash
    async def s_reset(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.reset(interaction.guild))


# ======================================================================= VOICE TOOLS
class VoiceToolsCog(commands.Cog, name="Voice"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def in_voice(member: discord.Member) -> Optional[discord.Embed]:
        if member.voice is None or member.voice.channel is None:
            return ui.warn("Not In Voice", f"{member.mention} is not in a voice channel.")
        return None

    async def do_edit(self, guild, actor, member: discord.Member, title: str, text: str, **changes) -> discord.Embed:
        problem = member_problem(guild, actor, member)
        if problem:
            return ui.error("Action Blocked", problem)
        absent = self.in_voice(member)
        if absent:
            return absent
        try:
            await member.edit(reason=f"Voice action by {actor}", **changes)
        except discord.Forbidden:
            return need_perm("Mute / Deafen / Move Members")
        return ui.success(title, text.format(member=member.mention))

    async def do_kick(self, guild, actor, member: discord.Member) -> discord.Embed:
        problem = member_problem(guild, actor, member)
        if problem:
            return ui.error("Action Blocked", problem)
        absent = self.in_voice(member)
        if absent:
            return absent
        try:
            await member.move_to(None, reason=f"Disconnected by {actor}")
        except discord.Forbidden:
            return need_perm("Move Members")
        return ui.success("Disconnected", f"{member.mention} was disconnected from voice.")

    async def do_move(self, guild, actor, member: discord.Member, channel: discord.VoiceChannel) -> discord.Embed:
        problem = member_problem(guild, actor, member)
        if problem:
            return ui.error("Action Blocked", problem)
        absent = self.in_voice(member)
        if absent:
            return absent
        try:
            await member.move_to(channel, reason=f"Moved by {actor}")
        except discord.Forbidden:
            return need_perm("Move Members")
        return ui.success("Member Moved", f"{member.mention} was moved to {channel.mention}.")

    async def do_moveall(self, actor, source: discord.VoiceChannel, target: discord.VoiceChannel) -> discord.Embed:
        moved = 0
        for member in list(source.members):
            try:
                await member.move_to(target, reason=f"Move all by {actor}")
                moved += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        return ui.success("Voice Channel Moved", f"**{moved}** member(s) moved from {source.mention} to {target.mention}.")

    # ---- dot commands
    @commands.command(name="vcmute", brief="Server-mute a member in voice.", extras={"category": "Voice", "usage": ".vcmute @user"})
    @commands.guild_only()
    @admin_prefix
    async def vcmute(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(embed=await self.do_edit(ctx.guild, ctx.author, member, "Member Muted", "{member} was muted in voice.", mute=True))

    @commands.command(name="vcunmute", brief="Remove a voice mute.", extras={"category": "Voice", "usage": ".vcunmute @user"})
    @commands.guild_only()
    @admin_prefix
    async def vcunmute(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(embed=await self.do_edit(ctx.guild, ctx.author, member, "Member Unmuted", "{member} was unmuted in voice.", mute=False))

    @commands.command(name="vcdeafen", brief="Server-deafen a member in voice.", extras={"category": "Voice", "usage": ".vcdeafen @user"})
    @commands.guild_only()
    @admin_prefix
    async def vcdeafen(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(embed=await self.do_edit(ctx.guild, ctx.author, member, "Member Deafened", "{member} was deafened in voice.", deafen=True))

    @commands.command(name="vcundeafen", brief="Remove a voice deafen.", extras={"category": "Voice", "usage": ".vcundeafen @user"})
    @commands.guild_only()
    @admin_prefix
    async def vcundeafen(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(embed=await self.do_edit(ctx.guild, ctx.author, member, "Member Undeafened", "{member} was undeafened in voice.", deafen=False))

    @commands.command(name="vckick", brief="Disconnect a member from voice.", extras={"category": "Voice", "usage": ".vckick @user"})
    @commands.guild_only()
    @admin_prefix
    async def vckick(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(embed=await self.do_kick(ctx.guild, ctx.author, member))

    @commands.command(name="vcmove", brief="Move a member: .vcmove @user #voice", extras={"category": "Voice", "usage": ".vcmove @user #voice"})
    @commands.guild_only()
    @admin_prefix
    async def vcmove(self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel) -> None:
        await ctx.send(embed=await self.do_move(ctx.guild, ctx.author, member, channel))

    @commands.command(name="vcmoveall", brief="Move everyone: .vcmoveall #from #to", extras={"category": "Voice", "usage": ".vcmoveall #from #to"})
    @commands.guild_only()
    @admin_prefix
    async def vcmoveall(self, ctx: commands.Context, source: discord.VoiceChannel, target: discord.VoiceChannel) -> None:
        await ctx.send(embed=await self.do_moveall(ctx.author, source, target))

    @commands.command(name="vclimit", brief="Set the user limit of your voice channel (0 = none).", extras={"category": "Voice", "usage": ".vclimit <0-99>"})
    @commands.guild_only()
    @admin_prefix
    async def vclimit(self, ctx: commands.Context, limit: int) -> None:
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            await ctx.send(embed=ui.warn("Not In Voice", "Join a voice channel first."))
            return
        limit = max(0, min(limit, 99))
        try:
            await voice.channel.edit(user_limit=limit, reason=f"Limit changed by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(embed=need_perm("Manage Channels"))
            return
        await ctx.send(embed=ui.success("Limit Updated", f"{voice.channel.mention} now allows **{limit or 'unlimited'}** members."))

    # ---- slash commands
    voice = app_commands.Group(name="voice", description="Voice moderation", guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @voice.command(name="mute", description="Server-mute a member in voice")
    @admin_slash
    async def s_mute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await ui.respond(interaction, await self.do_edit(interaction.guild, interaction.user, member, "Member Muted", "{member} was muted in voice.", mute=True))

    @voice.command(name="unmute", description="Remove a voice mute")
    @admin_slash
    async def s_unmute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await ui.respond(interaction, await self.do_edit(interaction.guild, interaction.user, member, "Member Unmuted", "{member} was unmuted in voice.", mute=False))

    @voice.command(name="deafen", description="Server-deafen a member in voice")
    @admin_slash
    async def s_deafen(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await ui.respond(interaction, await self.do_edit(interaction.guild, interaction.user, member, "Member Deafened", "{member} was deafened in voice.", deafen=True))

    @voice.command(name="undeafen", description="Remove a voice deafen")
    @admin_slash
    async def s_undeafen(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await ui.respond(interaction, await self.do_edit(interaction.guild, interaction.user, member, "Member Undeafened", "{member} was undeafened in voice.", deafen=False))

    @voice.command(name="kick", description="Disconnect a member from voice")
    @admin_slash
    async def s_kick(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await ui.respond(interaction, await self.do_kick(interaction.guild, interaction.user, member))

    @voice.command(name="move", description="Move a member to another voice channel")
    @admin_slash
    async def s_move(self, interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel) -> None:
        await ui.respond(interaction, await self.do_move(interaction.guild, interaction.user, member, channel))
