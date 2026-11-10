"""Shared permission checks and role-hierarchy helpers."""
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

admin_slash = app_commands.checks.has_permissions(administrator=True)
admin_prefix = commands.has_guild_permissions(administrator=True)


def is_admin(user) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def dangerous(role: discord.Role) -> bool:
    """Roles that must never be handed out automatically or through self roles."""
    p = role.permissions
    return any(
        (
            p.administrator,
            p.manage_guild,
            p.manage_roles,
            p.manage_channels,
            p.manage_webhooks,
            p.ban_members,
            p.kick_members,
            p.moderate_members,
            p.mention_everyone,
        )
    )


def role_problem(guild: discord.Guild, actor, role: discord.Role) -> Optional[str]:
    me = guild.me
    if role.is_default():
        return "The @everyone role cannot be used here."
    if role.managed:
        return "That role is managed by an integration or bot."
    if me is None or role >= me.top_role:
        return "That role is higher than or equal to my top role. Move my role above it."
    if isinstance(actor, discord.Member) and actor.id != guild.owner_id and role >= actor.top_role:
        return "That role is higher than or equal to your top role."
    return None


def member_problem(guild: discord.Guild, actor, target: discord.Member) -> Optional[str]:
    me = guild.me
    if target.id == guild.owner_id:
        return "The server owner cannot be targeted."
    if actor is not None and target.id == actor.id:
        return "You cannot use this on yourself."
    if me is not None and target.id == me.id:
        return "I cannot use this on myself."
    if me is not None and target.top_role >= me.top_role:
        return "That member's top role is higher than or equal to mine. Move my role above it."
    if isinstance(actor, discord.Member) and actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "That member's top role is higher than or equal to yours."
    return None
