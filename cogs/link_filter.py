from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from botcore.link_filter import LinkFilterConfig, contains_link
from botcore.moderation_embeds import build_link_blocked_embed


class LinkFilterCog(commands.Cog, name="Link Filter"):
    def __init__(
        self,
        bot: commands.Bot,
        config: LinkFilterConfig,
        delete_delay: int,
        log_channel_id: int,
    ) -> None:
        self.bot = bot
        self.config = config
        # Keep accidental short production values from making the warning
        # disappear before users can read it; zero still means keep forever.
        self.delete_delay = 0 if delete_delay == 0 else max(8, delete_delay)
        self.log_channel_id = log_channel_id

    def _emojis(self, guild_id: int) -> dict[str, str]:
        mass_cog = self.bot.get_cog("MassMentionCog")
        return dict(getattr(mass_cog, "emojis", {}).get(guild_id, {}))

    @staticmethod
    def _component_urls(components: list[Any]) -> list[str]:
        urls: list[str] = []
        for component in components:
            url = getattr(component, "url", None)
            if isinstance(url, str):
                urls.append(url)
            urls.extend(LinkFilterCog._component_urls(list(getattr(component, "children", ()))))
            urls.extend(LinkFilterCog._component_urls(list(getattr(component, "components", ()))))
        return urls

    def _message_links(self, message: discord.Message) -> tuple[bool, list[str]]:
        found, links = contains_link(message.content or "", self.config)
        extra = []
        for embed in message.embeds:
            if embed.url:
                extra.append(embed.url)
        extra.extend(self._component_urls(list(getattr(message, "components", ()))))
        for value in extra:
            extra_found, extra_links = contains_link(value, self.config)
            if extra_found:
                found = True
                links.extend(item for item in extra_links if item not in links)
        return found, links

    def _exempt(self, message: discord.Message) -> bool:
        member = message.author
        if not isinstance(member, discord.Member):
            return True
        if self.bot.user is not None and member.id == self.bot.user.id:
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return True
        if self.config.whitelisted_channel_ids and message.channel.id in self.config.whitelisted_channel_ids:
            return True
        return bool(self.config.whitelisted_role_ids & {role.id for role in member.roles})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self._exempt(message):
            return
        found, links = self._message_links(message)
        if not found:
            return
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Link filter delete skipped: {type(error).__name__}: {error}")
        await self._send_warning(message, links)

    async def _send_warning(self, message: discord.Message, links: list[str]) -> None:
        member = message.author
        embed = build_link_blocked_embed(
            user_id=member.id,
            emojis=self._emojis(message.guild.id),
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        try:
            warning = await message.channel.send(
                content=member.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Link filter warning could not be sent: {type(error).__name__}: {error}")
            return
        if self.delete_delay:
            await asyncio.sleep(self.delete_delay)
            try:
                await warning.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"Link filter warning cleanup skipped: {type(error).__name__}: {error}")
        safe_links = ", ".join(item.replace("://", "[:]//") for item in links)[:1000]
        logger = self.bot.get_cog("Security")
        if logger is not None and hasattr(logger, "write_log"):
            await logger.write_log(
                message.guild,
                discord.Embed(
                    title="Link blocked",
                    description=f"User: {member.mention}\nChannel: {message.channel.mention}\nLink: {safe_links}",
                    color=discord.Colour(0xF0B232),
                ),
            )
        elif self.log_channel_id:
            print(f"Link blocked in {message.guild.id}: {safe_links}")

    @app_commands.command(name="testlink", description="Preview the links-blocked warning.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def testlink(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        embed = build_link_blocked_embed(
            user_id=interaction.user.id,
            emojis=self._emojis(interaction.guild.id),
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkFilterCog(
        bot,
        bot.link_filter_config,
        bot.link_filter_delete_delay,
        bot.link_filter_log_channel_id,
    ))
