from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import discord
from discord import app_commands
from discord.ext import commands

from botcore.mass_mention import add_warning
from botcore.mass_mention_embed import build_mass_mention_embed

MASS_MENTION_CONFIG: dict[str, Any] = {
    "max_level": 4,
    "decay_hours": 24,
    "levels": {
        1: {"action": "warn"},
        2: {"action": "timeout", "timeout_minutes": 10},
        3: {"action": "kick"},
        4: {"action": "ban"},
    },
}
MASS_MENTION_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "kryo-embed"


class MassMentionCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        state: dict[str, Any],
        save,
        whitelist_role_ids: tuple[int, ...] = (),
        decay_hours: int = 24,
        live_animation: bool = False,
    ) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.whitelist_role_ids = frozenset(whitelist_role_ids)
        self.decay_hours = decay_hours
        self.live_animation = live_animation
        self.emojis: dict[int, dict[str, str]] = {}
        self._warning_lock = asyncio.Lock()
        self._manifest = self._load_manifest()

    @staticmethod
    def _load_manifest() -> list[dict[str, str]]:
        try:
            manifest = json.loads((ASSET_DIR / "emoji-manifest.json").read_text(encoding="utf-8"))
            return [item for item in manifest.get("emojis", []) if isinstance(item, dict)]
        except (OSError, ValueError, TypeError) as error:
            print(f"Mass mention emoji manifest could not be loaded: {type(error).__name__}: {error}")
            return []

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._load_guild_emojis(guild)

    async def _load_guild_emojis(self, guild: discord.Guild) -> None:
        values: dict[str, str] = {}
        existing = {emoji.name: emoji for emoji in guild.emojis}
        limit = getattr(guild, "emoji_limit", None)
        remaining_slots = max(0, limit - len(guild.emojis)) if isinstance(limit, int) else None
        can_manage_emojis = (
            guild.me is None or guild.me.guild_permissions.manage_emojis_and_stickers
        )
        missing_assets = [
            item for item in self._manifest
            if item.get("name") and item["name"] not in existing and item.get("file")
        ]
        if remaining_slots is not None and len(missing_assets) > remaining_slots:
            print(
                f"Guild {guild.id} may not have enough emoji slots for moderation animations "
                f"({len(missing_assets)} missing, {remaining_slots} free); static fallbacks will be used."
            )
        if missing_assets and not can_manage_emojis:
            print(
                f"Guild {guild.id} cannot upload animated moderation emojis: "
                "Manage Guild Expressions permission is missing; static fallbacks will be used."
            )
        for item in self._manifest:
            key = item.get("key")
            name = item.get("name")
            fallback = item.get("fallback", "")
            if not key or not name:
                continue
            emoji = existing.get(name)
            if emoji is None:
                path = ASSET_DIR / item.get("file", "")
                try:
                    if not path.is_file():
                        raise OSError(f"asset not found: {path.name}")
                    if remaining_slots == 0:
                        values[key] = fallback
                        continue
                    if not can_manage_emojis:
                        values[key] = fallback
                        continue
                    emoji = await guild.create_custom_emoji(
                        name=name,
                        image=path.read_bytes(),
                        reason="KRYO moderation and warning embed assets",
                    )
                    if remaining_slots is not None:
                        remaining_slots -= 1
                except (discord.Forbidden, discord.HTTPException, OSError) as error:
                    print(f"Mass mention emoji {name} unavailable in {guild.id}: {type(error).__name__}: {error}")
            if emoji is not None and emoji.animated:
                values[key] = f"<a:{name}:{emoji.id}>"
                values[f"{key}_URL"] = str(emoji.url)
            else:
                values[key] = str(emoji) if emoji else fallback
        self.emojis[guild.id] = values

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        member = message.author if isinstance(message.author, discord.Member) else None
        if guild is None or member is None or member.bot:
            return
        content = message.content or ""
        if not (getattr(message, "mention_everyone", False) or MASS_MENTION_RE.search(content)):
            return
        if member.guild_permissions.mention_everyone or member.guild_permissions.administrator:
            return
        if self.whitelist_role_ids and any(role.id in self.whitelist_role_ids for role in member.roles):
            return

        tried = []
        if re.search(r"@everyone\b", content, re.IGNORECASE) or getattr(message, "mention_everyone", False):
            tried.append("@everyone")
        if re.search(r"@here\b", content, re.IGNORECASE):
            tried.append("@here")
        if not tried:
            tried.append("@everyone")

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Mass mention message delete skipped in {guild.id}: {type(error).__name__}: {error}")

        async with self._warning_lock:
            level = add_warning(self.state, guild.id, member.id, self.decay_hours)
            self.save()

        embed = build_mass_mention_embed(
            user=member,
            level=level,
            max_level=MASS_MENTION_CONFIG["max_level"],
            tried=tried,
            emojis=self.emojis.get(guild.id, {}),
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        try:
            warning_message = await message.channel.send(
                content=member.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Mass mention warning could not be sent in {guild.id}: {type(error).__name__}: {error}")
            return

        await self._punish(guild, member, level)
        if self.live_animation:
            await self._animate(warning_message, member, level, tried)

    async def _punish(self, guild: discord.Guild, member: discord.Member, level: int) -> None:
        action = MASS_MENTION_CONFIG["levels"][level]["action"]
        reason = f"Mass mention warning level {level}/{MASS_MENTION_CONFIG['max_level']}"
        try:
            if action == "timeout":
                await member.timeout(
                    discord.utils.utcnow() + timedelta(minutes=10),
                    reason=reason,
                )
            elif action == "kick":
                await guild.kick(member, reason=reason)
            elif action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Mass mention punishment {action} skipped for {member.id}: {type(error).__name__}: {error}")

    async def _animate(self, warning_message: discord.Message, member: discord.Member, level: int, tried: list[str]) -> None:
        for index in range(3):
            await asyncio.sleep(1.2)
            try:
                emojis = dict(self.emojis.get(member.guild.id, {}))
                emojis["ARROW"] = "➤" * (index + 1)
                emojis["CHEVRONS"] = "›" * (index + 1)
                await warning_message.edit(
                    embed=build_mass_mention_embed(
                        user=member,
                        level=level,
                        max_level=MASS_MENTION_CONFIG["max_level"],
                        tried=tried,
                        emojis=emojis,
                        bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                    )
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

    @app_commands.command(name="testwarning", description="Preview a mass mention warning level.")
    @app_commands.guild_only()
    @app_commands.describe(level="Warning level to preview (1-4)")
    @app_commands.checks.has_permissions(administrator=True)
    async def testwarning(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 4]) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = interaction.user
        embed = build_mass_mention_embed(
            user=user,
            level=level,
            max_level=MASS_MENTION_CONFIG["max_level"],
            tried=["@everyone", "@here"],
            emojis=self.emojis.get(guild.id, {}),
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MassMentionCog(bot, bot.state, bot.save, bot.mass_mention_whitelist_role_ids, bot.mass_mention_decay_hours, bot.mass_mention_live_animation))
