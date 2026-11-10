from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

import discord
from discord.ext import commands

from botcore import ui


log = logging.getLogger(__name__)
DEFAULT_CATEGORIES = [
    {"key": "general", "label": "General Support", "description": "Ask the team for help.", "emoji": "🎫", "questions": []},
    {"key": "billing", "label": "Billing", "description": "Questions about plans and payments.", "emoji": "💳", "questions": []},
    {"key": "report", "label": "Report a User", "description": "Privately report a member.", "emoji": "🛡️", "questions": []},
]


class TicketPanelSelect(discord.ui.Select):
    def __init__(self, cog: TicketCog, guild_id: int | None = None) -> None:
        self.cog = cog
        super().__init__(
            custom_id="kryo:tickets:open",
            placeholder="Choose what you need help with",
            min_values=1,
            max_values=1,
            options=cog.category_options(guild_id),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
            return
        category = next(
            (
                item for item in self.cog.cfg(interaction.guild.id)["categories"]
                if item["key"] == self.values[0]
            ),
            None,
        )
        if category is None:
            await interaction.response.send_message(
                "That ticket category is no longer available. Please refresh the ticket panel.",
                ephemeral=True,
            )
            return
        questions = category.get("questions", [])
        if questions:
            await interaction.response.send_modal(TicketIntakeModal(self.cog, category))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.open_ticket(interaction, category, [])


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: TicketCog, guild_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect(cog, guild_id))


class TicketIntakeModal(discord.ui.Modal):
    def __init__(self, cog: TicketCog, category: dict[str, Any]) -> None:
        super().__init__(title=f"{category['label'][:28]} · Details", timeout=300)
        self.cog = cog
        self.category = category
        self.answers: list[tuple[str, str]] = []
        for index, question in enumerate(category.get("questions", [])[:5]):
            field = discord.ui.TextInput(
                label=question[:45],
                custom_id=f"answer_{index}",
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=500,
                placeholder="Add any useful details (optional)",
            )
            self.add_item(field)
            self.answers.append((question, ""))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
            return
        answers = [
            (question, str(self.children[index].value).strip())
            for index, (question, _answer) in enumerate(self.answers)
            if str(self.children[index].value).strip()
        ]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.open_ticket(interaction, self.category, answers)


class TicketControlsView(discord.ui.View):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(TicketClaimButton(cog))
        self.add_item(TicketAssignButton(cog))
        self.add_item(TicketCloseButton(cog))
        self.add_item(TicketDeleteButton(cog))


class TicketClosedView(discord.ui.View):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(TicketReopenButton(cog))
        self.add_item(TicketRatingButton(cog))
        self.add_item(TicketDeleteButton(cog))


class TicketClaimButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Claim ticket", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="kryo:tickets:claim")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.claim(interaction)


class TicketAssignButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Assign staff", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="kryo:tickets:assign")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.start_assignment(interaction)


class TicketAddMemberButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Add member", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="kryo:tickets:add-member")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.reject_extra_member_access(interaction)


class TicketRemoveMemberButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Remove member", emoji="➖", style=discord.ButtonStyle.secondary, custom_id="kryo:tickets:remove-member")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.reject_extra_member_access(interaction)


class TicketCloseButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="kryo:tickets:close")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.close_ticket(interaction)


class TicketReopenButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id="kryo:tickets:reopen")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.reopen_ticket(interaction)


class TicketRatingButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Rate support", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="kryo:tickets:rate")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.rate_ticket(interaction)


class TicketDeleteButton(discord.ui.Button):
    def __init__(self, cog: TicketCog) -> None:
        super().__init__(label="Delete channel", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="kryo:tickets:delete")
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_ticket(interaction)


class TicketUserSelect(discord.ui.UserSelect):
    def __init__(self, cog: TicketCog, action: str) -> None:
        self.cog = cog
        self.action = action
        super().__init__(
            placeholder="Choose a server member",
            min_values=1,
            max_values=1,
            custom_id=f"kryo:tickets:{action}-user",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = self.values[0]
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Choose a member of this server.", ephemeral=True)
            return
        if self.action == "assign":
            await self.cog.assign(interaction, member)
        else:
            await self.cog.reject_extra_member_access(interaction)


class TicketUserSelectView(discord.ui.View):
    def __init__(self, cog: TicketCog, action: str) -> None:
        super().__init__(timeout=60)
        self.add_item(TicketUserSelect(cog, action))


class TicketCog(commands.Cog, name="TicketCog"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._persistent_views_registered = False

    def cfg(self, guild_id: int) -> dict[str, Any]:
        settings = self.state.setdefault("settings", {}).setdefault(str(guild_id), {})
        cfg = settings.setdefault("tickets", {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("panel_channel_id", 0)
        cfg.setdefault("panel_message_id", 0)
        cfg.setdefault("ticket_category_id", 0)
        cfg.setdefault("staff_role_ids", [])
        cfg.setdefault("transcript_channel_id", 0)
        cfg.setdefault("panel_title", "How can we help?")
        cfg.setdefault("panel_description", "Choose a category below and our team will be with you shortly.")
        cfg.setdefault("panel_tagline", "PRIVATE · ORGANIZED · PERSONAL")
        cfg.setdefault("panel_tagline_description", "Your request is visible only to you and the support team. Select a category to get started.")
        cfg.setdefault("panel_next_title", "WHAT HAPPENS NEXT")
        cfg.setdefault("panel_next_description", "KRYO opens a private channel. Staff can claim, assign, and reply to your ticket.")
        cfg.setdefault("panel_footer", "KRYO · Select a category to get started")
        cfg.setdefault("panel_color", "#5865F2")
        cfg.setdefault("panel_image_url", "")
        cfg.setdefault("open_limit", 1)
        cfg.setdefault("categories", [dict(item) for item in DEFAULT_CATEGORIES])
        cfg.setdefault("records", {})
        if not isinstance(cfg["records"], dict):
            cfg["records"] = {}
        if not isinstance(cfg["categories"], list):
            cfg["categories"] = [dict(item) for item in DEFAULT_CATEGORIES]
        if not isinstance(cfg["staff_role_ids"], list):
            cfg["staff_role_ids"] = []
        return cfg

    def dashboard_payload(self, guild: discord.Guild) -> dict[str, Any]:
        cfg = self.cfg(guild.id)
        records = cfg["records"]
        category_labels = {
            item.get("key"): item.get("label")
            for item in cfg["categories"]
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        discovered = False
        for channel in getattr(guild, "text_channels", ()):
            match = re.search(
                r"(?:^|\s)KRYO-TICKET opener:(\d+) category:([a-z0-9_-]+)(?:$|\s)",
                str(getattr(channel, "topic", "") or ""),
            )
            if match is None:
                continue
            channel_id = str(channel.id)
            if isinstance(records.get(channel_id), dict):
                continue
            opener_id = int(match.group(1))
            category_key = match.group(2)
            opener = guild.get_member(opener_id)
            created_at = getattr(channel, "created_at", None)
            records[channel_id] = {
                "channel_id": channel.id,
                "opener_id": opener_id,
                "opener_name": opener.display_name if opener is not None else f"User {opener_id}",
                "category_key": category_key,
                "category_label": category_labels.get(category_key, category_key.replace("-", " ").title()),
                "status": "closed" if channel.name.startswith("closed-") else "open",
                "claimed_by": None,
                "assigned_to": None,
                "participants": [opener_id],
                "created_at": created_at.isoformat() if created_at is not None else "",
                "closed_at": None,
                "rating": None,
            }
            discovered = True
        if discovered:
            self.save()

        active = []
        for channel_id, record in records.items():
            if (
                not str(channel_id).isdigit()
                or not isinstance(record, dict)
                or record.get("status") != "open"
            ):
                continue
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            claimed_id = record.get("claimed_by")
            assigned_id = record.get("assigned_to")
            get_member = getattr(guild, "get_member", lambda _member_id: None)
            claimed_member = get_member(claimed_id) if isinstance(claimed_id, int) else None
            assigned_member = get_member(assigned_id) if isinstance(assigned_id, int) else None
            active.append({
                "channel_id": str(channel_id),
                "channel_name": channel.name,
                "url": f"https://discord.com/channels/{guild.id}/{channel_id}",
                "category": str(record.get("category_label", "Support")),
                "opener": str(record.get("opener_name", "Unknown member")),
                "created_at": str(record.get("created_at", "")),
                "claimed_by_name": claimed_member.display_name if claimed_member is not None else "",
                "assigned_to_name": assigned_member.display_name if assigned_member is not None else "",
            })
        active.sort(key=lambda ticket: ticket["created_at"], reverse=True)
        return {
            "active_ticket_count": len(active),
            "active_tickets": active[:50],
        }

    def category_options(self, guild_id: int | None = None) -> list[discord.SelectOption]:
        categories = self.cfg(guild_id)["categories"] if guild_id is not None else list(DEFAULT_CATEGORIES)
        options = []
        for item in categories[:25]:
            emoji = item.get("emoji") or None
            if emoji:
                try:
                    emoji = discord.PartialEmoji.from_str(emoji)
                except (TypeError, ValueError):
                    emoji = None
            options.append(discord.SelectOption(
                label=item["label"][:100],
                value=item["key"],
                description=item.get("description", "")[:100] or None,
                emoji=emoji,
            ))
        return options or [discord.SelectOption(label="General Support", value="general")]

    def get_record(self, channel: discord.abc.GuildChannel) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if channel.guild is None:
            return None
        cfg = self.cfg(channel.guild.id)
        record = cfg["records"].get(str(channel.id))
        return (cfg, record) if isinstance(record, dict) else None

    def has_ticket_staff_role(self, member: discord.Member, cfg: dict[str, Any]) -> bool:
        staff_ids = {int(value) for value in cfg["staff_role_ids"] if str(value).isdigit()}
        return any(role.id in staff_ids for role in member.roles)

    def is_staff(self, member: discord.Member, cfg: dict[str, Any]) -> bool:
        return (
            member.guild_permissions.manage_channels
            or member.guild_permissions.administrator
            or self.has_ticket_staff_role(member, cfg)
        )

    def is_ticket_staff_or_admin(self, member: discord.Member, cfg: dict[str, Any]) -> bool:
        return (
            member.guild_permissions.administrator
            or self.has_ticket_staff_role(member, cfg)
        )

    async def interaction_member(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This action is only available in a server.", ephemeral=True)
            return None
        return interaction.user

    async def staff_only(self, interaction: discord.Interaction) -> tuple[discord.Member, dict[str, Any], dict[str, Any]] | None:
        member = await self.interaction_member(interaction)
        if member is None:
            return None
        ticket = self.get_record(interaction.channel) if isinstance(interaction.channel, discord.abc.GuildChannel) else None
        if ticket is None:
            await interaction.response.send_message("This channel is not a KRYO ticket.", ephemeral=True)
            return None
        cfg, record = ticket
        if not self.is_staff(member, cfg):
            await interaction.response.send_message("Only configured ticket staff can use this action.", ephemeral=True)
            return None
        return member, cfg, record

    def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cfg(guild.id)
        embed = ui.security(cfg["panel_title"], cfg["panel_description"])
        if re.fullmatch(r"#[0-9a-fA-F]{6}", cfg["panel_color"]):
            embed.color = discord.Color(int(cfg["panel_color"][1:], 16))
        embed.set_author(name=f"{guild.name}  •  KRYO SUPPORT", icon_url=guild.icon.url if guild.icon else None)
        icon = ui.EMBED_EMOJI_MENTIONS.get("kx_ui_info", "🎫")
        tagline = cfg["panel_tagline"]
        tagline_description = cfg["panel_tagline_description"]
        if tagline or tagline_description:
            embed.add_field(
                name=f"{icon}  {tagline}" if tagline else "\u200b",
                value=tagline_description or "\u200b",
                inline=False,
            )
        next_title = cfg["panel_next_title"]
        next_description = cfg["panel_next_description"]
        if next_title or next_description:
            embed.add_field(
                name=next_title or "\u200b",
                value=next_description or "\u200b",
                inline=False,
            )
        if cfg["panel_footer"]:
            embed.set_footer(text=cfg["panel_footer"])
        icon_url = ui.EMBED_ICON_URLS.get("kx_ui_info")
        if icon_url:
            embed.set_thumbnail(url=icon_url)
        elif self.bot.user is not None:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        image_url = cfg.get("panel_image_url")
        if isinstance(image_url, str) and image_url.startswith("https://"):
            embed.set_image(url=image_url)
        return embed

    @staticmethod
    def ticket_overwrites(
        guild: discord.Guild,
        opener: discord.Member,
        cfg: dict[str, Any],
    ) -> dict[Any, discord.PermissionOverwrite]:
        bot_member = guild.me
        if bot_member is None:
            raise RuntimeError("The bot member is not available for ticket permission setup.")
        staff_role_ids = {
            int(value) for value in cfg["staff_role_ids"]
            if str(value).isdigit()
        }
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            ),
            opener: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        for role in guild.roles:
            if role.is_default():
                continue
            if role.id in staff_role_ids:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                    attach_files=True,
                    embed_links=True,
                )
            else:
                overwrites[role] = discord.PermissionOverwrite(view_channel=False)
        return overwrites

    async def secure_existing_ticket(
        self,
        channel: discord.TextChannel,
        record: dict[str, Any],
        cfg: dict[str, Any],
    ) -> None:
        opener_id = int(record["opener_id"])
        allowed_member_ids = {opener_id}
        if channel.guild.me is not None:
            allowed_member_ids.add(channel.guild.me.id)
        overwrites = {
            target: overwrite
            for target, overwrite in channel.overwrites.items()
            if isinstance(target, discord.Role) or target.id in allowed_member_ids
        }
        opener = channel.guild.get_member(opener_id)
        if opener is None:
            opener = discord.Object(id=opener_id)
        overwrites.update(self.ticket_overwrites(channel.guild, opener, cfg))
        await channel.edit(
            overwrites=overwrites,
            reason="Restrict ticket access to its opener and support staff",
        )

    async def publish_panel(self, guild: discord.Guild) -> discord.Message:
        cfg = self.cfg(guild.id)
        if not cfg["enabled"]:
            raise ValueError("Enable ticket support and save its settings before publishing the panel.")
        try:
            channel_id = int(cfg["panel_channel_id"])
        except (TypeError, ValueError):
            channel_id = 0
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Choose a valid text channel for the ticket panel.")
        if not cfg["categories"]:
            raise ValueError("Add at least one ticket category before publishing the panel.")
        old_message = None
        try:
            old_message_id = int(cfg.get("panel_message_id", 0))
        except (TypeError, ValueError):
            old_message_id = 0
        if old_message_id:
            try:
                old_message = await channel.fetch_message(old_message_id)
            except discord.NotFound:
                cfg["panel_message_id"] = 0
                old_message = None
        if old_message is None:
            old_message = await self.discover_existing_panel(guild, channel)
        if old_message is not None:
            await old_message.edit(embed=self.panel_embed(guild), view=TicketPanelView(self, guild.id))
            message = old_message
        else:
            message = await channel.send(embed=self.panel_embed(guild), view=TicketPanelView(self, guild.id))
        cfg["panel_message_id"] = message.id
        self.save()
        return message

    async def discover_existing_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | None = None,
    ) -> discord.Message | None:
        cfg = self.cfg(guild.id)
        if cfg.get("panel_message_id"):
            return None
        if channel is None:
            try:
                channel_id = int(cfg.get("panel_channel_id", 0))
            except (TypeError, ValueError):
                channel_id = 0
            candidate = guild.get_channel(channel_id) if channel_id else None
            channel = candidate if isinstance(candidate, discord.TextChannel) else None
        if channel is None or guild.me is None:
            return None
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_message_history:
            return None
        bot_user_id = self.bot.user.id if self.bot.user is not None else None
        async for message in channel.history(limit=100):
            if bot_user_id is not None and message.author.id != bot_user_id:
                continue
            has_ticket_selector = any(
                getattr(component, "custom_id", None) == "kryo:tickets:open"
                for row in message.components
                for component in getattr(row, "children", ())
            )
            if not has_ticket_selector:
                continue
            cfg["panel_channel_id"] = channel.id
            cfg["panel_message_id"] = message.id
            self.save()
            return message
        return None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._persistent_views_registered:
            return
        self.bot.add_view(TicketPanelView(self))
        for guild in self.bot.guilds:
            cfg = self.cfg(guild.id)
            for channel_id, record in cfg["records"].items():
                try:
                    channel = guild.get_channel(int(channel_id))
                except (TypeError, ValueError):
                    channel = None
                if isinstance(channel, discord.TextChannel):
                    try:
                        await self.secure_existing_ticket(channel, record, cfg)
                    except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as error:
                        log.warning("Could not enforce private access for ticket channel %s: %s", channel.id, error)
                try:
                    message_id = int(record.get("control_message_id", 0))
                except (TypeError, ValueError):
                    continue
                if not message_id:
                    continue
                view = TicketControlsView(self) if record.get("status") == "open" else TicketClosedView(self)
                self.bot.add_view(view, message_id=message_id)
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(view=view)
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException) as error:
                    log.warning("Could not refresh ticket controls for channel %s: %s", channel.id, error)
        self._persistent_views_registered = True

    async def open_ticket(
        self,
        interaction: discord.Interaction,
        category: dict[str, Any],
        answers: list[tuple[str, str]],
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.followup.send("Tickets can only be opened in a server.", ephemeral=True)
            return
        cfg = self.cfg(guild.id)
        if not cfg["enabled"]:
            await interaction.followup.send("Ticket support is currently unavailable.", ephemeral=True)
            return
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            active = [
                item for item in cfg["records"].values()
                if item.get("opener_id") == member.id and item.get("status") == "open"
            ]
            if len(active) >= cfg["open_limit"]:
                channel = guild.get_channel(active[0]["channel_id"])
                location = f" in {channel.mention}" if channel is not None else ""
                await interaction.followup.send(
                    f"You already have the maximum number of open tickets{location}.",
                    ephemeral=True,
                )
                return
            me = guild.me
            if me is None or not me.guild_permissions.manage_channels:
                await interaction.followup.send("KRYO needs Manage Channels permission to create private tickets.", ephemeral=True)
                return
            overwrites = self.ticket_overwrites(guild, member, cfg)
            category_channel = None
            try:
                category_id = int(cfg["ticket_category_id"])
            except (TypeError, ValueError):
                category_id = 0
            if category_id:
                candidate = guild.get_channel(category_id)
                if isinstance(candidate, discord.CategoryChannel):
                    category_channel = candidate
            slug = re.sub(r"[^a-z0-9-]+", "-", member.display_name.lower()).strip("-")[:24] or str(member.id)
            key = re.sub(r"[^a-z0-9-]+", "-", category["key"].lower()).strip("-")[:18] or "ticket"
            try:
                channel = await guild.create_text_channel(
                    f"{key}-{slug}",
                    category=category_channel,
                    overwrites=overwrites,
                    topic=f"KRYO-TICKET opener:{member.id} category:{category['key']}",
                    reason=f"Support ticket opened by {member} ({member.id})",
                )
            except discord.Forbidden:
                await interaction.followup.send("KRYO cannot create private ticket channels. Check its Manage Channels permission.", ephemeral=True)
                return
            except discord.HTTPException as error:
                log.warning("Ticket channel creation failed in guild %s: %s", guild.id, error)
                await interaction.followup.send("Discord could not create your ticket. Please try again.", ephemeral=True)
                return

            record = {
                "channel_id": channel.id,
                "opener_id": member.id,
                "opener_name": member.display_name,
                "category_key": category["key"],
                "category_label": category["label"],
                "status": "open",
                "claimed_by": None,
                "assigned_to": None,
                "participants": [member.id],
                "user_submitted_details": any(answer for _question, answer in answers),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "closed_at": None,
                "rating": None,
            }
            cfg["records"][str(channel.id)] = record
            embed = ui.security(f"{category.get('emoji', '🎫')}  {category['label']}", "A private support room is ready. Share the details below and our team will take it from here.")
            embed.set_author(name=f"{guild.name}  •  KRYO SUPPORT", icon_url=guild.icon.url if guild.icon else None)
            embed.add_field(name="TICKET", value=f"`{channel.name}`", inline=True)
            embed.add_field(name="OPENED BY", value=member.mention, inline=True)
            for question, answer in answers:
                embed.add_field(name=question[:256], value=answer[:1024], inline=False)
            embed.set_footer(text="KRYO  •  Keep all updates in this private ticket")
            try:
                message = await channel.send(
                    content=member.mention,
                    embed=embed,
                    view=TicketControlsView(self),
                    allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
                )
                record["control_message_id"] = message.id
                self.save()
            except discord.HTTPException as error:
                cfg["records"].pop(str(channel.id), None)
                try:
                    await channel.delete(reason="Could not initialize KRYO ticket controls")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not clean up uninitialized ticket channel %s", channel.id)
                log.warning("Ticket panel initialization failed in guild %s: %s", guild.id, error)
                await interaction.followup.send("The ticket channel was created but could not be initialized. Please contact staff.", ephemeral=True)
                return
            await interaction.followup.send(f"Your private ticket is ready: {channel.mention}", ephemeral=True)

    async def claim(self, interaction: discord.Interaction) -> None:
        member = await self.interaction_member(interaction)
        if member is None:
            return
        ticket = self.get_record(interaction.channel) if isinstance(interaction.channel, discord.abc.GuildChannel) else None
        if ticket is None:
            await interaction.response.send_message("This channel is not a KRYO ticket.", ephemeral=True)
            return
        cfg, record = ticket
        if member.id == record.get("opener_id") or not self.has_ticket_staff_role(member, cfg):
            await interaction.response.send_message(
                "Only configured ticket staff can claim this ticket.",
                ephemeral=True,
            )
            return
        if record.get("status") != "open":
            await interaction.response.send_message("This ticket is closed.", ephemeral=True)
            return
        record["claimed_by"] = member.id
        self.save()
        await interaction.response.send_message(
            embed=ui.success("Ticket Claimed", f"{member.mention} is now handling this ticket."),
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )

    async def start_assignment(self, interaction: discord.Interaction) -> None:
        staff = await self.staff_only(interaction)
        if staff is None:
            return
        member, cfg, _record = staff
        if not self.is_ticket_staff_or_admin(member, cfg):
            await interaction.response.send_message(
                "Only configured ticket staff or server administrators can assign this ticket.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Choose the staff member who will handle this ticket.",
            view=TicketUserSelectView(self, "assign"),
            ephemeral=True,
        )

    async def assign(self, interaction: discord.Interaction, target: discord.Member) -> None:
        staff = await self.staff_only(interaction)
        if staff is None:
            return
        actor, cfg, record = staff
        if record.get("status") != "open":
            await interaction.response.send_message("This ticket is closed.", ephemeral=True)
            return
        if not self.is_ticket_staff_or_admin(actor, cfg):
            await interaction.response.send_message(
                "Only configured ticket staff or server administrators can assign this ticket.",
                ephemeral=True,
            )
            return
        if not self.is_ticket_staff_or_admin(target, cfg):
            await interaction.response.send_message("That member does not have a configured ticket staff role.", ephemeral=True)
            return
        record["assigned_to"] = target.id
        record["claimed_by"] = target.id
        self.save()
        await interaction.response.send_message(
            embed=ui.info("Ticket Assigned", f"{target.mention} was assigned by {actor.mention}."),
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )

    async def reject_extra_member_access(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Tickets are private to the opener and configured support staff. Additional members cannot be added.",
            ephemeral=True,
        )

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        member = await self.interaction_member(interaction)
        if member is None:
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This ticket channel is unavailable.", ephemeral=True)
            return
        ticket = self.get_record(channel)
        if ticket is None:
            await interaction.response.send_message("This channel is not a KRYO ticket.", ephemeral=True)
            return
        cfg, record = ticket
        if not self.is_staff(member, cfg):
            await interaction.response.send_message("Only configured ticket staff or server administrators can close this ticket.", ephemeral=True)
            return
        if record.get("status") != "open":
            await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            transcript = await self.make_transcript(channel, record)
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("Ticket transcript could not be generated for channel %s: %s", channel.id, error)
            await interaction.followup.send(
                "Could not read the ticket history to create a transcript. Check KRYO's Read Message History permission and try again.",
                ephemeral=True,
            )
            return
        transcript_channel = self.resolve_text_channel(channel.guild, cfg.get("transcript_channel_id", 0))
        transcript_configured = bool(cfg.get("transcript_channel_id"))
        transcript_sent = False
        if transcript_channel is not None:
            try:
                await transcript_channel.send(
                    embed=ui.security(
                        f"Ticket Transcript  ·  {record['category_label']}",
                        f"**Ticket:** {channel.mention}\n**Opened by:** <@{record['opener_id']}>\n"
                        f"**Closed by:** {member.mention}\n**Claimed by:** "
                        + (f"<@{record['claimed_by']}>" if record.get("claimed_by") else "Unclaimed"),
                    ),
                    file=discord.File(transcript, filename=f"kryo-ticket-{channel.id}.txt"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                transcript_sent = True
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning("Ticket transcript could not be sent for channel %s: %s", channel.id, error)
        record["status"] = "closed"
        record["closed_at"] = datetime.now(timezone.utc).isoformat()
        archived = True
        try:
            opener = channel.guild.get_member(record["opener_id"])
            if opener is not None:
                await channel.set_permissions(opener, send_messages=False, add_reactions=False)
            new_name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"[:95]
            await channel.edit(name=new_name, reason=f"Ticket closed by {member} ({member.id})")
        except discord.Forbidden:
            archived = False
            log.warning("Ticket closed in state but could not archive permissions for channel %s", channel.id)
        except discord.HTTPException as error:
            archived = False
            log.warning("Ticket archive update failed for channel %s: %s", channel.id, error)
        self.save()
        try:
            closed_embed = ui.warn(
                "Ticket Closed",
                "This conversation is now archived. The opener can rate the support they received.",
            )
            if interaction.message is not None and interaction.message.channel.id == channel.id:
                await interaction.message.edit(embed=closed_embed, view=TicketClosedView(self))
                closed_message = interaction.message
            else:
                closed_message = await channel.send(
                    embed=closed_embed,
                    view=TicketClosedView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            record["control_message_id"] = closed_message.id
            self.save()
        except discord.HTTPException as error:
            log.warning("Ticket close notice failed for channel %s: %s", channel.id, error)
            try:
                closed_message = await channel.send(
                    embed=closed_embed,
                    view=TicketClosedView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                record["control_message_id"] = closed_message.id
                self.save()
            except discord.HTTPException as send_error:
                log.warning("Ticket close controls could not be posted for channel %s: %s", channel.id, send_error)
                await interaction.followup.send(
                    "Ticket closed, but its control message could not be updated. Staff may need to manage it manually.",
                    ephemeral=True,
                )
                return
        result = "Ticket closed."
        if transcript_sent:
            result += " The HTML transcript was sent to the staff log channel."
        elif not transcript_configured:
            result += " No transcript was sent because a staff transcript log channel is not configured."
        elif transcript_channel is None:
            result += " The configured transcript log channel is not available."
        else:
            result += " The transcript could not be delivered to the configured staff log channel."
        if not archived:
            result += " KRYO could not fully archive the channel; check its Manage Channels permission."
        await interaction.followup.send(result, ephemeral=True)

    async def reopen_ticket(self, interaction: discord.Interaction) -> None:
        staff = await self.staff_only(interaction)
        if staff is None:
            return
        member, _cfg, record = staff
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or record.get("status") != "closed":
            await interaction.response.send_message("This ticket cannot be reopened.", ephemeral=True)
            return
        opener = channel.guild.get_member(record["opener_id"])
        if opener is None:
            await interaction.response.send_message("The ticket opener is no longer in this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await channel.set_permissions(opener, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        except discord.Forbidden:
            await interaction.followup.send("KRYO cannot restore access to this ticket.", ephemeral=True)
            return
        except discord.HTTPException as error:
            log.warning("Ticket reopen failed for channel %s: %s", channel.id, error)
            await interaction.followup.send("Discord could not reopen this ticket.", ephemeral=True)
            return
        if channel.name.startswith("closed-"):
            try:
                await channel.edit(name=channel.name[7:], reason=f"Ticket reopened by {member} ({member.id})")
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning("Ticket reopened but channel rename failed for %s: %s", channel.id, error)
        record["status"] = "open"
        record["closed_at"] = None
        self.save()
        try:
            reopened_embed = ui.success("Ticket Reopened", f"{member.mention} reopened this support conversation.")
            if interaction.message is not None and interaction.message.channel.id == channel.id:
                await interaction.message.edit(embed=reopened_embed, view=TicketControlsView(self))
                message = interaction.message
            else:
                message = await channel.send(
                    embed=reopened_embed,
                    view=TicketControlsView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            log.warning("Could not edit ticket reopen controls in channel %s", channel.id)
            try:
                message = await channel.send(
                    embed=reopened_embed,
                    view=TicketControlsView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as send_error:
                log.warning("Could not post ticket reopen controls in channel %s: %s", channel.id, send_error)
                await interaction.followup.send("Ticket access was restored, but its controls could not be posted.", ephemeral=True)
                return
        except discord.HTTPException as error:
            log.warning("Ticket reopen notice failed for channel %s: %s", channel.id, error)
            try:
                message = await channel.send(
                    embed=reopened_embed,
                    view=TicketControlsView(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as send_error:
                log.warning("Could not post ticket reopen controls in channel %s: %s", channel.id, send_error)
                await interaction.followup.send("Ticket access was restored, but its controls could not be posted.", ephemeral=True)
                return
        record["control_message_id"] = message.id
        self.save()
        await interaction.followup.send("Ticket reopened.", ephemeral=True)

    async def rate_ticket(self, interaction: discord.Interaction) -> None:
        member = await self.interaction_member(interaction)
        if member is None:
            return
        ticket = self.get_record(interaction.channel) if isinstance(interaction.channel, discord.abc.GuildChannel) else None
        if ticket is None:
            await interaction.response.send_message("This channel is not a KRYO ticket.", ephemeral=True)
            return
        cfg, record = ticket
        if member.id != record.get("opener_id") or record.get("status") != "closed":
            await interaction.response.send_message("Only the opener can rate a closed ticket.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketRatingModal(self, cfg, record))

    async def save_rating(
        self,
        interaction: discord.Interaction,
        cfg: dict[str, Any],
        record: dict[str, Any],
        rating: int,
        comment: str,
    ) -> None:
        if record.get("rating") is not None:
            await interaction.response.send_message("You have already rated this ticket.", ephemeral=True)
            return
        record["rating"] = {
            "score": rating,
            "comment": comment[:500],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()
        log_channel = self.resolve_text_channel(interaction.guild, cfg.get("transcript_channel_id", 0))
        if log_channel is not None:
            try:
                await log_channel.send(embed=ui.gold(
                    "Ticket Feedback",
                    f"**Ticket:** <#{record['channel_id']}>\n**Rating:** {'⭐' * rating}\n"
                    f"**Feedback:** {comment.strip() or 'No written feedback'}",
                ), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning("Ticket rating log failed for channel %s: %s", record["channel_id"], error)
        await interaction.response.send_message("Thanks for rating your support experience.", ephemeral=True)

    async def delete_ticket(self, interaction: discord.Interaction) -> None:
        member = await self.interaction_member(interaction)
        if member is None:
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This ticket channel is unavailable.", ephemeral=True)
            return
        ticket = self.get_record(channel)
        if ticket is None:
            await interaction.response.send_message("This channel is not a KRYO ticket.", ephemeral=True)
            return
        cfg, record = ticket
        if not self.is_staff(member, cfg):
            await interaction.response.send_message("Only configured ticket staff or server administrators can delete this ticket.", ephemeral=True)
            return
        if record.get("status") != "closed":
            await interaction.response.send_message("Close the ticket before deleting its channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await channel.delete(reason=f"Ticket deleted by {member} ({member.id})")
        except discord.Forbidden:
            await interaction.followup.send("KRYO cannot delete this ticket channel.", ephemeral=True)
            return
        except discord.HTTPException as error:
            log.warning("Ticket deletion failed for channel %s: %s", channel.id, error)
            await interaction.followup.send("Discord could not delete this ticket channel.", ephemeral=True)
            return
        cfg["records"].pop(str(channel.id), None)
        self.save()
        await interaction.followup.send("Ticket channel deleted. The transcript remains in the configured log.", ephemeral=True)

    async def make_transcript(self, channel: discord.TextChannel, record: dict[str, Any]) -> io.BytesIO:
        messages = [message async for message in channel.history(limit=500, oldest_first=True)]
        rows = [
            f"KRYO SUPPORT TRANSCRIPT — {channel.name}",
            f"Category: {record.get('category_label', 'Support')}",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "=" * 72,
        ]
        for message in messages:
            timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            rows.extend(("", f"[{timestamp}] {message.author.display_name}"))
            rows.append(message.clean_content or "(no text)")
            rows.extend(f"Attachment: {item.filename} — {item.url}" for item in message.attachments)
        rows.extend(("", "=" * 72))
        return io.BytesIO("\n".join(rows).encode("utf-8"))

    @staticmethod
    def resolve_text_channel(guild: discord.Guild, raw_id: Any) -> discord.TextChannel | None:
        try:
            channel_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        cfg = self.cfg(member.guild.id)
        for channel_id, record in list(cfg["records"].items()):
            if record.get("opener_id") == member.id and record.get("status") == "open":
                channel = member.guild.get_channel(int(channel_id))
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.set_permissions(member, send_messages=False)
                    except (discord.Forbidden, discord.HTTPException) as error:
                        log.warning("Could not restrict departed ticket opener %s: %s", member.id, error)


class TicketRatingModal(discord.ui.Modal):
    def __init__(self, cog: TicketCog, cfg: dict[str, Any], record: dict[str, Any]) -> None:
        super().__init__(title="Rate your support experience", timeout=300)
        self.cog = cog
        self.cfg = cfg
        self.record = record
        self.score = discord.ui.TextInput(
            label="Rating from 1 to 5",
            placeholder="5",
            min_length=1,
            max_length=1,
        )
        self.feedback = discord.ui.TextInput(
            label="Anything else you'd like us to know?",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.score)
        self.add_item(self.feedback)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            score = int(str(self.score.value))
        except ValueError:
            await interaction.response.send_message("Choose a whole-number rating from 1 to 5.", ephemeral=True)
            return
        if not 1 <= score <= 5:
            await interaction.response.send_message("Choose a rating from 1 to 5.", ephemeral=True)
            return
        await self.cog.save_rating(interaction, self.cfg, self.record, score, str(self.feedback.value))
