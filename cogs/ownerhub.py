"""Owner Hub cog: central tracking and monitoring for K.R.Y.O across all guilds."""
import datetime
import json
import os
import threading
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

_STORE_DIR = Path(os.getenv("OWNER_HUB_STORE_DIR", "/app/data"))
STORE_PATH = _STORE_DIR / "owner_hub_guilds.json"
_STORE_LOCK = threading.Lock()

CATEGORIES = {
    "owner_control": "OWNER CONTROL",
    "security_monitor": "SECURITY MONITOR",
    "bot_system": "BOT SYSTEM",
}

CHANNELS = {
    "server-invites": "owner_control",
    "owner-dashboard": "owner_control",
    "server-joins": "owner_control",
    "server-leaves": "owner_control",
    "server-history": "owner_control",
    "anti-nuke-alerts": "security_monitor",
    "anti-raid-alerts": "security_monitor",
    "anti-webhook-alerts": "security_monitor",
    "moderation-alerts": "security_monitor",
    "protection-status": "security_monitor",
    "bot-status": "bot_system",
    "bot-errors": "bot_system",
}


def _load() -> dict:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    with _STORE_LOCK:
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(STORE_PATH)


class OwnerHub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        from main import CONFIG
        self.hub_guild_id = CONFIG.owner_hub_guild_id
        self.log_channel_id = CONFIG.owner_hub_log_channel_id
        self.dashboard_channel_id = CONFIG.owner_hub_dashboard_channel_id
        self.protected_owners = CONFIG.global_protected_owner_ids
        self.guilds = _load()
        ch = self.guilds.get("_channels", {}) if isinstance(self.guilds.get("_channels"), dict) else {}
        if not self.dashboard_channel_id and ch.get("dashboard"):
            self.dashboard_channel_id = ch["dashboard"]
        if not self.log_channel_id and ch.get("log"):
            self.log_channel_id = ch["log"]
        self.channel_ids = ch

    def _is_owner(self, user_id: int) -> bool:
        return user_id in self.protected_owners

    def _in_hub(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and interaction.guild.id == self.hub_guild_id

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return False
        if not self._in_hub(interaction):
            await interaction.response.send_message("This command is only available in the Owner Hub.", ephemeral=True)
            return False
        return True

    def _channel_overwrites(self, guild, bot_member):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
        }
        for uid in self.protected_owners:
            member = guild.get_member(uid)
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        return overwrites

    async def _ensure_channels(self):
        if not self.hub_guild_id:
            return
        guild = self.bot.get_guild(self.hub_guild_id)
        if guild is None:
            return
        bot_member = guild.me
        overwrites = self._channel_overwrites(guild, bot_member)
        # Categories
        cat_map = {}
        for key, name in CATEGORIES.items():
            existing = discord.utils.get(guild.categories, name=name)
            if existing is None:
                try:
                    cat = await guild.create_category(name, overwrites=overwrites, reason="Owner Hub setup")
                except discord.HTTPException as e:
                    print(f"[ownerhub] failed to create {ch_name}: {e}")
                    continue
            else:
                cat = existing
            cat_map[key] = cat
        # Channels
        channel_ids = self.guilds.get("_channels", {}) if isinstance(self.guilds.get("_channels"), dict) else {}
        for ch_name, cat_key in CHANNELS.items():
            cat = cat_map.get(cat_key)
            existing = discord.utils.get(guild.text_channels, name=ch_name)
            if existing is None:
                try:
                    ch = await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites, reason="Owner Hub setup")
                except discord.HTTPException as e:
                    print(f"[ownerhub] failed to create {ch_name}: {e}")
                    continue
            else:
                ch = existing
                if cat is not None and ch.category != cat:
                    try:
                        await ch.edit(category=cat)
                    except discord.HTTPException:
                        pass
            channel_ids[ch_name] = ch.id
            # Update convenience attributes
            if ch_name == "owner-dashboard":
                self.dashboard_channel_id = ch.id
            if ch_name == "security-logs":
                self.log_channel_id = ch.id
        self.guilds["_channels"] = channel_ids
        _save(self.guilds)

    async def _joins_channel(self):
        guild = self.bot.get_guild(self.hub_guild_id)
        if guild is None:
            return None
        ids = self.guilds.get("_channels", {}) if isinstance(self.guilds.get("_channels"), dict) else {}
        ch_id = ids.get("server-joins")
        if not ch_id:
            return None
        ch = guild.get_channel(ch_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(ch_id)
            except Exception:
                return None
        return ch

    async def _leaves_channel(self):
        guild = self.bot.get_guild(self.hub_guild_id)
        if guild is None:
            return None
        ids = self.guilds.get("_channels", {}) if isinstance(self.guilds.get("_channels"), dict) else {}
        ch_id = ids.get("server-leaves")
        if not ch_id:
            return None
        ch = guild.get_channel(ch_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(ch_id)
            except Exception:
                return None
        return ch

    async def _post_server_joins_snapshot(self):
        ch = await self._joins_channel()
        if ch is None:
            return
        posted = set(self.guilds.get("_joins_posted", []) or [])
        for g in list(self.bot.guilds):
            if g.id == self.hub_guild_id:
                continue
            if str(g.id) in posted:
                continue
            data = self.guilds.get(str(g.id), {}) if isinstance(self.guilds.get(str(g.id)), dict) else {}
            joined = data.get("first_seen") or data.get("last_seen") or "unknown"
            try:
                await ch.send("**" + g.name + "** (`" + str(g.id) + "`)\nMembers: " + str(g.member_count) + "\nJoined: " + str(joined))
            except Exception:
                pass
            posted.add(str(g.id))
        self.guilds["_joins_posted"] = list(posted)
        _save(self.guilds)

    async def _post_join_event(self, guild, timestamp):
        ch = await self._joins_channel()
        if ch is None:
            return
        try:
            await ch.send("**" + guild.name + "** (`" + str(guild.id) + "`)\nMembers: " + str(guild.member_count) + "\nJoined: " + str(timestamp))
        except Exception:
            pass
        posted = set(self.guilds.get("_joins_posted", []) or [])
        posted.add(str(guild.id))
        self.guilds["_joins_posted"] = list(posted)
        _save(self.guilds)

    async def _post_leave_event(self, gid, name, timestamp):
        ch = await self._leaves_channel()
        if ch is None:
            return
        try:
            await ch.send("**" + str(name) + "** (`" + str(gid) + "`)\nLeft at: " + str(timestamp))
        except Exception:
            pass
    def _reconcile(self):
        now = datetime.datetime.utcnow().isoformat()
        for guild in self.bot.guilds:
            gid = str(guild.id)
            if gid == "_channels":
                continue
            if gid not in self.guilds:
                self.guilds[gid] = {
                    "name": guild.name,
                    "owner_id": guild.owner_id,
                    "member_count": guild.member_count,
                    "first_seen": now,
                    "status": "Already Present - Initial Sync",
                }
            else:
                self.guilds[gid]["name"] = guild.name
                self.guilds[gid]["member_count"] = guild.member_count
                self.guilds[gid]["last_seen"] = now
                if self.guilds[gid].get("status") == "Removed":
                    self.guilds[gid]["status"] = "Joined"
        _save(self.guilds)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_channels()
        self._reconcile()
        await self._post_server_invites()
        await self._post_server_joins_snapshot()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        now = datetime.datetime.utcnow().isoformat()
        self.guilds[str(guild.id)] = {
            "name": guild.name,
            "owner_id": guild.owner_id,
            "member_count": guild.member_count,
            "first_seen": now,
            "status": "Joined",
        }
        _save(self.guilds)
        await self._post_join_event(guild, now)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        now = datetime.datetime.utcnow().isoformat()
        gid = str(guild.id)
        cached = self.guilds.get(gid, {}) if isinstance(self.guilds.get(gid), dict) else {}
        name = cached.get("name", getattr(guild, "name", "?"))
        if gid in self.guilds and isinstance(self.guilds[gid], dict):
            self.guilds[gid]["status"] = "Removed"
            self.guilds[gid]["removed_at"] = now
            self.guilds[gid]["name"] = name
        _save(self.guilds)
        await self._post_leave_event(gid, name, now)

    @app_commands.command(name="ownerhub", description="Show the owner dashboard")
    async def ownerhub(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        joined = [g for g in self.guilds.values() if isinstance(g, dict) and g.get("status") in ("Joined", "Already Present - Initial Sync")]
        removed = [g for g in self.guilds.values() if isinstance(g, dict) and g.get("status") == "Removed"]
        embed = discord.Embed(title="K.R.Y.O Owner Hub", color=0x5865F2)
        embed.add_field(name="Currently Joined", value=str(len(joined)), inline=True)
        embed.add_field(name="Removed", value=str(len(removed)), inline=True)
        embed.add_field(name="Total Tracked", value=str(len([g for g in self.guilds.values() if isinstance(g, dict)])), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="servers", description="List connected servers")
    async def servers(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        joined = [(gid, g) for gid, g in self.guilds.items() if isinstance(g, dict) and g.get("status") in ("Joined", "Already Present - Initial Sync")]
        lines = [f"`{gid}` — {g.get('name', '?')} ({g.get('member_count', '?')} members)" for gid, g in joined[:25]]
        embed = discord.Embed(title="Connected Servers", description="\n".join(lines) or "None", color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="serverhistory", description="Show previously recorded servers")
    async def serverhistory(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        lines = [f"`{gid}` — {g.get('name', '?')} [{g.get('status', '?')}]" for gid, g in list(self.guilds.items())[:25] if isinstance(g, dict)]
        embed = discord.Embed(title="Server History", description="\n".join(lines) or "None", color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="refreshhub", description="Refresh dashboard and registry")
    async def refreshhub(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await self._ensure_channels()
        self._reconcile()
        await self._post_server_invites()
        await interaction.response.send_message("Refreshed.", ephemeral=True)

    @app_commands.command(name="protectedservers", description="List fully protected servers")
    async def protectedservers(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("Protection status check not yet implemented.", ephemeral=True)

    @app_commands.command(name="partialservers", description="List partially protected servers")
    async def partialservers(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("Protection status check not yet implemented.", ephemeral=True)

    @app_commands.command(name="unprotectedservers", description="List servers needing attention")
    async def unprotectedservers(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("Protection status check not yet implemented.", ephemeral=True)

    @app_commands.command(name="serverinfo", description="Detailed info for a guild")
    async def serverinfo(self, interaction: discord.Interaction, guild_id: str):
        if not await self._owner_check(interaction):
            return
        g = self.guilds.get(guild_id)
        if not g or not isinstance(g, dict):
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        embed = discord.Embed(title=g.get("name", "?"), color=0x5865F2)
        embed.add_field(name="ID", value=guild_id, inline=True)
        embed.add_field(name="Status", value=g.get("status", "?"), inline=True)
        embed.add_field(name="Members", value=str(g.get("member_count", "?")), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="securitylogs", description="Review recent security events")
    async def securitylogs(self, interaction: discord.Interaction):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_message("Security log review not yet implemented.", ephemeral=True)


    async def _post_server_invites(self):
        guild = self.bot.get_guild(self.hub_guild_id)
        if guild is None:
            return
        ids = self.guilds.get("_channels", {}) if isinstance(self.guilds.get("_channels"), dict) else {}
        ch_id = ids.get("server-invites")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(ch_id)
            except Exception:
                return
        posted = set(self.guilds.get("_invites", []) or [])
        for g in list(self.bot.guilds):
            if g.id == self.hub_guild_id:
                continue
            if str(g.id) in posted:
                continue
            invite_url = None
            reason = "no suitable channel"
            for ch in g.text_channels:
                perms = ch.permissions_for(g.me)
                if perms.create_instant_invite and perms.view_channel:
                    try:
                        inv = await ch.create_invite(max_age=0, max_uses=0, reason="Owner Hub server invite")
                        invite_url = inv.url
                        reason = "created"
                    except Exception as e:
                        reason = "failed: " + str(e)
                    break
            try:
                await channel.send("**" + g.name + "** (`" + str(g.id) + "`)\nInvite: " + (invite_url or "N/A") + "\nStatus: " + reason)
            except Exception:
                pass
            posted.add(str(g.id))
        self.guilds["_invites"] = list(posted)
        _save(self.guilds)

async def setup(bot):
    await bot.add_cog(OwnerHub(bot))
