import asyncio
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union, cast

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui
from botcore.security import lockdown as lockdown_mod
from botcore.security import image_promo as image_promo_mod
from botcore.security import risk as risk_mod
from botcore.security import rules as rules_mod
from botcore.security import snapshots as snap_mod
from botcore.moderation_embeds import (
    build_ban_embed,
    build_caps_embed,
    build_link_blocked_embed,
    build_moderation_action_dm,
    build_promo_removed_embed,
    build_unban_embed,
    moderation_emojis_for,
)
from botcore.whitelist_embed import (
    build_already_whitelisted_embed,
    build_not_whitelisted_embed,
    build_whitelist_updated_embed,
)

ANTINUKE_BANNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "antinuke_premium_preview.gif"
PROTECTIONS_BANNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "protections_premium_preview.gif"
PROTECTIONS_PREVIEW_PATH = Path(__file__).resolve().parents[1] / "assets" / "protections_embed_preview.png"
ANTINUKE_BANNER_FILENAME = "antinuke_premium_preview.gif"
PROTECTIONS_BANNER_FILENAME = "protections_premium_preview.gif"
PROTECTIONS_PREVIEW_FILENAME = "protections_embed_preview.png"
ANTINUKE_BANNER_DURATION = 7.1
ENABLE_GIF_PATH = Path(__file__).resolve().parents[1] / "assets" / "antinuke_enable.gif"
ENABLE_PNG_PATH = Path(__file__).resolve().parents[1] / "assets" / "antinuke_enabled.png"
ENABLE_GIF_NAME = "antinuke_enable.gif"
ENABLE_PNG_NAME = "antinuke_enabled.png"
ENABLE_ACCENT = discord.Colour(0x8B5CF6)

A = discord.AuditLogAction

# key -> (label, description, punishable, default punishment)
# Built from the protection registry so there is exactly one place that decides
# what a protection is called, how bad it is and whether it is punishable.
MODULES: dict[str, tuple[str, str, bool, str]] = {
    key: (
        protection.label,
        protection.description,
        key in rules_mod.PUNISHABLE_MODULES,
        protection.punishment,
    )
    for key, protection in rules_mod.PROTECTIONS.items()
}
NUKE_MODULES = rules_mod.NUKE_MODULES
CHAT_MODULES = rules_mod.CHAT_MODULES
PUNISHABLE = [key for key in rules_mod.PUNISHABLE_MODULES]
NEW_MODULE_KEYS = list(rules_mod.NEW_MODULES)
PUNISHMENTS = rules_mod.PUNISHMENTS
ANTINUKE_BANNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "antinuke_premium_preview.gif"
PROTECTIONS_BANNER_PATH = Path(__file__).resolve().parents[1] / "assets" / "protections_premium_preview.gif"
ANTINUKE_BANNER_FILENAME = "antinuke_premium_preview.gif"
PROTECTIONS_BANNER_FILENAME = "protections_premium_preview.gif"
ANTINUKE_BANNER_DURATION = 7.1

ACTION_MODULE = {
    A.channel_create: "antichannel",
    A.channel_delete: "antichannel",
    A.channel_update: "antichannel",
    A.role_create: "antirole",
    A.role_delete: "antirole",
    A.role_update: "antirole",
    A.ban: "antiban",
    A.kick: "antiban",
    A.member_prune: "antiban",
    A.bot_add: "antibot",
    A.webhook_create: "antiwebhook",
    A.guild_update: "antiserver",
    A.integration_create: "antiintegration",
    A.integration_update: "antiintegration",
    A.integration_delete: "antiintegration",
}
# Destructive, instantly-reversible actions are punished on the first attempt.
# Ban/kick/prune stay on the threshold because they only mean something in bulk.
INSTANT_ACTIONS = {A.channel_delete, A.role_delete, A.bot_add, A.webhook_create, A.integration_create, A.guild_update}

# Guild settings that can be restored from the audit log without extra lookups.
GUILD_TEXT_SETTINGS = {
    "name": ("name", str),
    "description": ("description", str),
    "verification_level": ("verification_level", int),
    "afk_timeout": ("afk_timeout", int),
    "explicit_content_filter": ("explicit_content_filter", int),
    "default_notifications": ("default_notifications", int),
    "preferred_locale": ("preferred_locale", str),
}
GUILD_ASSETS = (("icon", "icon", "icons"), ("splash", "splash", "splashes"), ("banner", "banner", "banners"))
# Boost ticks, feature flags and other harmless guild edits must never trigger a punishment.
GUILD_GUARDED = frozenset(GUILD_TEXT_SETTINGS) | {"afk_channel_id", "system_channel_id", "icon", "splash", "banner"}
CHANNEL_SETTINGS = ("name", "topic", "nsfw", "bitrate", "user_limit", "rtc_region", "rate_limit_per_user")

#: Discord allows 25 options per select and 25 choices per command option, so the
#: panel and the whitelist list are grouped by category instead of listing all 34
#: protections flat.
MAX_MENU_ITEMS = 25
CATEGORY_LABELS = {
    "all": "All protections",
    "nuke": "Core anti-nuke",
    "chat": "Chat filters",
    "permission": "Permission abuse",
    "structure": "Structure changes",
    "asset": "Emojis, stickers and sounds",
    "invite": "Invites",
}
CATEGORY_ORDER = ["all", "nuke", "permission", "structure", "asset", "invite", "chat"]


def modules_in_category(category: str) -> list[str]:
    if category == "all":
        return list(MODULES)
    return [key for key in MODULES if rules_mod.PROTECTIONS[key].category == category]

URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
INVITE_RE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me|discord\.io)/([\w-]+)", re.I)
EVERYONE_RE = re.compile(r"@(everyone|here)", re.I)

EVERYONE_WINDOW = 60
MENTION_STRIKE_LIMIT = 4
WARNING_DELETE_AFTER = 5
RAID_JOIN_LIMIT = 6
RAID_WINDOW = 10
RAID_LOCKDOWN_SECONDS = 300
ALT_ACCOUNT_AGE_DAYS = 7

#: Per-guild options for the member role-grant protection, stored under
#: settings[guild_id]["role_protection"] in data.json. Every key is
#: optional; missing keys fall back to these defaults.
ROLE_PROTECTION_DEFAULTS: dict[str, Any] = {
    #: Master switch for the member role-permission guard. It can be
    #: disabled from the dashboard when the server needs to pause the
    #: protection temporarily without removing the module toggle.
    "enabled": True,
    #: Permission keys that make a role grant an instant incident.
    "dangerous_permissions": list(rules_mod.ROLE_DANGEROUS_PERMISSIONS),
    #: Seconds to wait before re-reading an audit entry that arrived
    #: without a role diff.
    "audit_retry_delay": 1.5,
    #: Ignore audit entries older than this (replays after reconnects).
    "audit_max_age": 10.0,
    #: Non-dangerous role changes that trigger the punishment. A warning
    #: is logged at grant_punish_at - 1.
    "grant_punish_at": 3,
    #: Window (seconds) in which non-dangerous changes are counted.
    "grant_window": 300.0,
    #: Only explicitly whitelisted user IDs bypass this protection,
    #: never whitelisted roles or names.
    "whitelist_strict": True,
    #: Role IDs the onboarding / role-selection flow may hand to members
    #: (game roles). The member is recorded as the actor for these,
    #: which is legitimate.
    "self_assignable_roles": [],
    #: Honour the self_assignable_roles list for self-assigned roles.
    "ignore_onboarding_roles": True,
}
#: Roles that are not cached are fetched from the API at most this many
#: times per event, so a flood of entries cannot hammer the API.
ROLE_RESOLVE_FETCH_LIMIT = 5

admin_only_slash = app_commands.checks.has_permissions(administrator=True)
admin_only_prefix = commands.has_guild_permissions(administrator=True)


def parse_state(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    value = value.lower()
    if value in ("on", "enable", "enabled", "true", "yes", "1"):
        return True
    if value in ("off", "disable", "disabled", "false", "no", "0"):
        return False
    return None


def is_admin(user) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


WL_OPTIONS = [("all", "All protections")] + [(key, CATEGORY_LABELS[key]) for key in CATEGORY_ORDER if key != "all"]
WL_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in WL_OPTIONS]


class WhitelistSelect(discord.ui.Select):
    def __init__(self, cog: "SecurityCog", guild: discord.Guild, kind: str, placeholder: str) -> None:
        super().__init__(
            placeholder=placeholder,
            options=[discord.SelectOption(label=label, value=key, emoji=ui.SPIDER) for key, label in WL_OPTIONS],
        )
        self.cog = cog
        self.guild = guild
        self.kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        await ui.respond(interaction, self.cog.whitelist_list_embed(self.guild, self.values[0], self.kind), ephemeral=True)


class WhitelistView(discord.ui.View):
    def __init__(self, cog: "SecurityCog", guild: discord.Guild) -> None:
        super().__init__(timeout=300)
        self.add_item(WhitelistSelect(cog, guild, "users", "View Whitelisted Users"))
        self.add_item(WhitelistSelect(cog, guild, "roles", "View Whitelisted Roles"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await ui.respond(
            interaction,
            ui.error("Access Denied", "Only server administrators can use this panel."),
            ephemeral=True,
        )
        return False


class PunishmentPanel(discord.ui.View):
    """Admin panel: pick a category, then a protection, then a punishment.

    Discord allows only 25 options per select and there are 34 protections, so
    the list is grouped by category. "All protections" still exists and still
    applies one punishment to everything, exactly as before.
    """

    def __init__(self, cog: "SecurityCog", guild: discord.Guild) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.category = "all"
        self.selected = "all"
        self._fill_modules()

    def _fillable(self, category: str) -> list[str]:
        keys = [key for key in modules_in_category(category) if key in PUNISHABLE]
        return keys[:MAX_MENU_ITEMS]

    def _fill_modules(self) -> None:
        if self.category == "all":
            options = [discord.SelectOption(label="Every protection", value="all", emoji=ui.SPIDER)]
        else:
            label = CATEGORY_LABELS[self.category]
            options = [discord.SelectOption(label=f"Everything in {label}", value="all", emoji=ui.SPIDER)]
            options += [
                discord.SelectOption(label=MODULES[key][0], value=key, emoji=ui.SPIDER)
                for key in self._fillable(self.category)
            ][:MAX_MENU_ITEMS]
        self.module_select.options = options
        self.refresh_defaults()

    def refresh_defaults(self) -> None:
        for option in self.module_select.options:
            option.default = option.value == self.selected

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await ui.respond(
            interaction,
            ui.error("Access Denied", "Only server administrators can use this panel."),
            ephemeral=True,
        )
        return False

    @discord.ui.select(
        placeholder="First Menu · Choose a category",
        options=[discord.SelectOption(label=CATEGORY_LABELS[key], value=key, emoji=ui.SPIDER) for key in CATEGORY_ORDER],
    )
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.category = select.values[0]
        self.selected = "all"
        self._fill_modules()
        await interaction.response.edit_message(
            embed=self.cog.panel_embed(self.guild, self.category, self.selected), view=self
        )

    @discord.ui.select(
        placeholder="Second Menu · Choose a protection",
        options=[discord.SelectOption(label="Every protection", value="all", emoji=ui.SPIDER)],
    )
    async def module_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.selected = select.values[0]
        self.refresh_defaults()
        await interaction.response.edit_message(
            embed=self.cog.panel_embed(self.guild, self.category, self.selected), view=self
        )

    @discord.ui.select(
        placeholder="Third Menu · Choose the punishment",
        options=[
            discord.SelectOption(label="Ban", value="ban", emoji=ui.SPIDER, description="Permanently ban the offender"),
            discord.SelectOption(label="Kick", value="kick", emoji=ui.SPIDER, description="Remove the offender from the server"),
            discord.SelectOption(label="Timeout", value="timeout", emoji=ui.SPIDER, description="Mute the offender (28 days, 10 min for spam)"),
        ],
    )
    async def punishment_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        choice = select.values[0]
        targets = self._fillable(self.category) if self.selected == "all" else [self.selected]
        punishments = self.cog.cfg(self.guild.id)["punishments"]
        for key in targets:
            punishments[key] = choice
        self.cog.save()
        await interaction.response.edit_message(
            embed=self.cog.panel_embed(self.guild, self.category, self.selected), view=self
        )


class AllProtectionsView(discord.ui.View):
    def __init__(self, cog: "SecurityCog", guild: discord.Guild) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        panel_button = discord.ui.Button(
            label="Open Panel",
            style=discord.ButtonStyle.primary,
            custom_id=f"antinuke:panel:{guild.id}",
        )
        panel_button.callback = self.open_panel
        self.add_item(panel_button)
        protections_button = discord.ui.Button(
            label=f"View All {len(MODULES)} Protections",
            style=discord.ButtonStyle.secondary,
            custom_id=f"antinuke:protections:{guild.id}",
        )
        protections_button.callback = self.view_all_protections
        self.add_item(protections_button)
        whitelist_button = discord.ui.Button(
            label="Whitelist",
            style=discord.ButtonStyle.secondary,
            custom_id=f"antinuke:whitelist:{guild.id}",
        )
        whitelist_button.callback = self.open_whitelist
        self.add_item(whitelist_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await ui.respond(
            interaction,
            ui.error("Access Denied", "Only server administrators can use this panel."),
            ephemeral=True,
        )
        return False

    async def open_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.panel_embed(self.guild),
            view=PunishmentPanel(self.cog, self.guild),
            ephemeral=True,
        )

    async def view_all_protections(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.all_protections_overview_embed(self.guild),
            ephemeral=True,
        )

    async def open_whitelist(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.whitelist_embed(self.guild),
            view=WhitelistView(self.cog, self.guild),
            ephemeral=True,
        )


class AllProtectionsLayout(discord.ui.LayoutView):
    def __init__(self, cog: "SecurityCog", guild: discord.Guild, armed: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        banner = ENABLE_PNG_NAME if armed else ENABLE_GIF_NAME
        children: list[Any] = [
            discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{banner}")),
        ]
        if armed:
            children.append(discord.ui.TextDisplay(f"### \u2728 K.R.Y.O Security Dashboard"))
            children.append(discord.ui.TextDisplay(cog.enable_summary_text(guild)))
            children.append(discord.ui.Separator())
            panel_button = discord.ui.Button(
                label="Open Panel",
                style=discord.ButtonStyle.primary,
                custom_id=f"antinuke:panel:{guild.id}",
            )
            panel_button.callback = self.open_panel
            protections_button = discord.ui.Button(
                label=f"View All {len(MODULES)}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"antinuke:protections:{guild.id}",
            )
            protections_button.callback = self.view_all_protections
            whitelist_button = discord.ui.Button(
                label="Whitelist",
                style=discord.ButtonStyle.secondary,
                custom_id=f"antinuke:whitelist:{guild.id}",
            )
            whitelist_button.callback = self.open_whitelist
            children.append(discord.ui.ActionRow(panel_button, protections_button, whitelist_button))
        else:
            children.append(
                discord.ui.TextDisplay(
                    f"### \u2728 Enabling Protections\nTurning on {len(MODULES)} protections for **{guild.name}**."
                )
            )
        children.append(discord.ui.TextDisplay(f"-# {ui.BRAND}  \u2022  Security Control  \u2022  {guild.id}"))
        self.add_item(discord.ui.Container(*children, accent_colour=ENABLE_ACCENT))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await ui.respond(
            interaction,
            ui.error("Access Denied", "Only server administrators can use this panel."),
            ephemeral=True,
        )
        return False

    async def open_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.panel_embed(self.guild),
            view=PunishmentPanel(self.cog, self.guild),
            ephemeral=True,
        )

    async def view_all_protections(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.all_protections_overview_embed(self.guild),
            ephemeral=True,
        )

    async def open_whitelist(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.cog.whitelist_embed(self.guild),
            view=WhitelistView(self.cog, self.guild),
            ephemeral=True,
        )


class SecurityCog(commands.Cog, name="Security"):
    def __init__(
        self,
        bot: commands.Bot,
        state: dict,
        save: Callable[[], None],
        has_tier: Callable[[int, str], bool],
        trusted_role_management_bot_ids: tuple[int, ...] = (),
        global_protected_owner_ids: tuple[int, ...] = (),
        promo_warning_delete_delay: int = 8,
    ) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.has_tier = has_tier
        self.trusted_role_management_bot_ids = frozenset(trusted_role_management_bot_ids)
        self.global_protected_owner_ids = frozenset(global_protected_owner_ids)
        self.promo_warning_delete_delay = max(0, promo_warning_delete_delay)
        self.actions: defaultdict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        self.message_times: defaultdict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.mention_strikes: defaultdict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.last_messages: defaultdict[tuple[int, int], deque[str]] = defaultdict(lambda: deque(maxlen=3))
        self.joins: defaultdict[int, deque[tuple[float, int]]] = defaultdict(deque)
        self.lockdown_until: dict[int, float] = {}
        self.recent_punish: dict[tuple[int, int], float] = {}
        self._dynamic_slash: list[app_commands.Command | app_commands.Group] = []
        self._dynamic_prefix: list[commands.Command] = []
        #: guild_id -> recent security events for `/seclogs`
        self.security_log: defaultdict[int, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=25))
        #: (guild, audit id) currently being handled, stops a double punishment
        self._forwarded_alerts: set[tuple[int, int]] = set()
        self._handling = set()
        self._lockdown_task: Optional[asyncio.Task] = None
        self._background: set[asyncio.Task] = set()
        self._promo_image_scans = asyncio.Semaphore(2)
        #: guild_id -> bucket key -> (user id set, role id set). See _whitelist_sets.
        self._wl_cache: dict[int, dict[str, tuple[set[int], set[int]]]] = {}
        self.risk = risk_mod.RiskEngine(
            threshold_reader=self.security_thresholds, window_reader=self.security_window
        )
        self.snapshots = snap_mod.SnapshotStore(self.guild_settings, self.save)
        self.lockdowns = lockdown_mod.LockdownManager(state_for=self.guild_settings, save=self.save)

    # ------------------------------------------------------------------ setup
    async def cog_load(self) -> None:
        self._spawn(
            asyncio.to_thread(image_promo_mod.warmup),
            "warmup:promotional-image-ocr",
        )
        # Only the original protections keep a dedicated toggle command; the 20
        # newer ones are switched through `/protection toggle` and
        # `.protection toggle`, which avoids 40 near-identical commands.
        for module in rules_mod.LEGACY_MODULES:
            label = MODULES[module][0]
            if module == "antipromo":
                slash = self._make_antipromo_slash(module, label)
            else:
                slash = self._make_slash_toggle(module, label)
            self.bot.tree.add_command(slash)
            self._dynamic_slash.append(slash)
            prefix = self._make_prefix_toggle(module, label)
            self.bot.add_command(prefix)
            self._dynamic_prefix.append(prefix)

        # /antinuke group (matches .antinuke prefix group)
        group = app_commands.Group(
            name="antinuke",
            description="Antinuke: on, off, status, panel, limits.",
            default_permissions=discord.Permissions(administrator=True),
            guild_only=True,
        )

        @group.command(name="on", description="Turn every protection on.")
        @admin_only_slash
        async def antinuke_on(interaction: discord.Interaction):
            await self.slash_toggle_all(interaction, True)

        @group.command(name="off", description="Turn every protection off.")
        @admin_only_slash
        async def antinuke_off(interaction: discord.Interaction):
            await self.slash_toggle_all(interaction, False)

        @group.command(name="status", description="Show protection status.")
        @admin_only_slash
        async def antinuke_status(interaction: discord.Interaction):
            guild = interaction.guild
            if guild is None:
                return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
            await ui.respond(interaction, self.status_embed(guild))

        @group.command(name="panel", description="Choose Ban, Kick or Timeout per protection.")
        @admin_only_slash
        async def antinuke_panel(interaction: discord.Interaction):
            guild = interaction.guild
            if guild is None:
                return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
            await ui.respond(interaction, self.panel_embed(guild), view=PunishmentPanel(self, guild))

        @group.command(name="limits", description="Set action limit and window.")
        @admin_only_slash
        @app_commands.describe(actions="Actions allowed before punishment (1-10)", window_seconds="Time window in seconds (5-60)")
        async def antinuke_limits(interaction: discord.Interaction, actions: int = 3, window_seconds: int = 10):
            guild = interaction.guild
            if guild is None:
                return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
            if not 1 <= actions <= 10 or not 5 <= window_seconds <= 60:
                await ui.respond(interaction, ui.warn("Invalid Limits", "Actions must be 1-10 and the window 5-60 seconds."))
                return
            await ui.respond(interaction, self.set_limits(guild, actions, window_seconds))

        self.bot.tree.add_command(group)
        self._dynamic_slash.append(group)

        self._lockdown_task = asyncio.create_task(self._lockdown_sweeper())

    async def cog_unload(self) -> None:
        if self._lockdown_task is not None:
            self._lockdown_task.cancel()
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        for slash in self._dynamic_slash:
            self.bot.tree.remove_command(slash.name)
        for prefix in self._dynamic_prefix:
            self.bot.remove_command(prefix.name)

    def _spawn(self, coro, label: str) -> asyncio.Task:
        """Run protection work in the background without ever losing it.

        A bare `asyncio.create_task` keeps no reference to the task, so the event
        loop may drop it before it finishes, and any exception it raises is never
        retrieved. Both failure modes are silent: a member that should have been
        banned simply is not, with no error anywhere. Holding the task until it
        settles and printing its exception makes the same work reliable.
        """
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(lambda finished, what=label: self._report_task(finished, what))
        return task

    @staticmethod
    def _report_task(task: asyncio.Task, label: str) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            print(f"[security] background task '{label}' failed: {type(error).__name__}: {error}")

    async def _lockdown_sweeper(self) -> None:
        """Lift a lockdown once its timer runs out, even if nobody is online."""
        while not self.bot.is_closed():
            try:
                for guild in list(self.bot.guilds):
                    result = await lockdown_mod.sweep_expired(self.lockdowns, guild)
                    if result is None:
                        continue
                    await self.write_log(guild, ui.warn("LOCKDOWN LIFTED", result.state.summary()), dm_owner=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[security] lockdown sweeper failed: {type(error).__name__}: {error}")
            await asyncio.sleep(20)

    def security_thresholds(self, guild_id: int) -> dict[str, int]:
        value = self.guild_settings(guild_id).get("security_thresholds")
        return value if isinstance(value, dict) else {}

    def security_window(self, guild_id: int) -> float:
        try:
            return max(10.0, min(3600.0, float(self.guild_settings(guild_id).get("security_window", risk_mod.DEFAULT_WINDOW))))
        except (TypeError, ValueError):
            return float(risk_mod.DEFAULT_WINDOW)

    def set_thresholds(self, guild_id: int, thresholds: dict[str, int], window: Optional[float] = None) -> None:
        settings = self.guild_settings(guild_id)
        settings["security_thresholds"] = {key: int(value) for key, value in thresholds.items() if key in MODULES}
        if window is not None:
            settings["security_window"] = float(window)
        self.save()
        self.risk.clear_guild(guild_id)

    async def recover_on_ready(self) -> None:
        """Reload lockdown state after a restart.

        Without this a restart silently drops an active lockdown, which is the
        cheapest possible bypass: crash the bot and the defences go away.
        """
        if self.bot.user is None:
            return
        for guild in self.bot.guilds:
            try:
                state = self.lockdowns.restore_on_startup(guild)
                if state is not None:
                    if state.expired():
                        result = await lockdown_mod.sweep_expired(self.lockdowns, guild)
                        if result is not None:
                            await self.write_log(
                                guild,
                                ui.warn("LOCKDOWN LIFTED", result.state.summary()),
                                dm_owner=True,
                            )
                        continue
                    await self.write_log(
                        guild,
                        ui.warn(
                            "LOCKDOWN STILL ACTIVE",
                            f"Reloaded after a restart. **{state.remaining() // 60} minutes** left.\n"
                            f"**Reason:** {state.reason or 'unknown'}",
                        ),
                        dm_owner=True,
                    )
            except Exception as error:
                print(f"[security] could not restore lockdown for {guild.id}: {type(error).__name__}: {error}")

    def _make_slash_toggle(self, module: str, label: str) -> app_commands.Command:
        async def callback(interaction: discord.Interaction, state: Literal["on", "off"]):
            await self.slash_toggle(interaction, module, state == "on")

        callback.__name__ = f"slash_{module}"
        callback = admin_only_slash(app_commands.default_permissions(administrator=True)(app_commands.guild_only()(callback)))
        return app_commands.Command(name=module, description=f"Turn {label} on or off", callback=callback)

    def _make_antipromo_slash(self, module: str, label: str) -> app_commands.Command:
        async def callback(
            interaction: discord.Interaction,
            state: Optional[Literal["on", "off"]] = None,
            punishment: Optional[Literal["ban", "kick", "timeout"]] = None,
            duration: Optional[int] = None,
        ):
            guild = interaction.guild
            if guild is None:
                return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
            if punishment is not None:
                self.cfg(guild.id)["punishments"][module] = punishment
                self.save()
            if duration is not None:
                self.cfg(guild.id)["timeouts"][module] = duration
                self.save()
            if state is not None:
                await ui.respond(interaction, self.toggle_module(guild, module, state == "on"), ephemeral=False)
                return
            await ui.respond(
                interaction,
                ui.info("Anti Promotion", f"Current punishment: `{PUNISHMENTS[self.punishment(guild.id, module)]}` · timeout: {self.timeout_minutes(guild.id, module)} min"),
                ephemeral=True,
            )

        callback.__name__ = f"slash_{module}"
        callback = admin_only_slash(app_commands.default_permissions(administrator=True)(app_commands.guild_only()(callback)))
        return app_commands.Command(name=module, description=f"Turn {label} on or off", callback=callback)

    def _make_prefix_toggle(self, module: str, label: str) -> commands.Command:
        async def callback(ctx: commands.Context, state: Optional[str] = None):
            guild = ctx.guild
            if guild is None:
                await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
                return
            value = parse_state(state)
            if value is None:
                await ctx.send(embed=ui.warn("Usage", f"`.{module} on` or `.{module} off`"))
                return
            await ctx.send(embed=self.toggle_module(guild, module, value))

        callback.__name__ = f"prefix_{module}"
        callback = admin_only_prefix(commands.guild_only()(callback))
        return commands.Command(
            callback,
            name=module,
            brief=f"Turn {label} on or off.",
            extras={"category": "Auto Mod" if module in CHAT_MODULES else "Anti Nuke", "usage": f".{module} on|off"},
        )

    # --------------------------------------------------------------- settings
    def guild_settings(self, guild_id: int) -> dict:
        return self.state["settings"].setdefault(str(guild_id), {})

    def cfg(self, guild_id: int) -> dict:
        sec = self.guild_settings(guild_id).setdefault("security", {})
        sec.setdefault("modules", {})
        sec.setdefault("punishments", {})
        if not isinstance(sec.get("timeouts"), dict):
            sec["timeouts"] = {}
        return sec

    def enabled(self, guild_id: int, module: str) -> bool:
        if module in {"antirolepermedit", "antiadmingrant"} and not self.role_protection(guild_id).get("enabled", True):
            return False
        return bool(self.cfg(guild_id)["modules"].get(module, False))

    def timeout_minutes(self, guild_id: int, module: str) -> int:
        configured = self.cfg(guild_id)["timeouts"].get(module)
        default = (
            rules_mod.DEFAULT_SPAM_TIMEOUT_MINUTES
            if module == "antispam"
            else 10
            if module == "antipromo"
            else rules_mod.DEFAULT_TIMEOUT_MINUTES
        )
        if type(configured) is int and rules_mod.MIN_TIMEOUT_MINUTES <= configured <= rules_mod.MAX_TIMEOUT_MINUTES:
            return configured
        return default

    def punishment(self, guild_id: int, module: str) -> str:
        value = self.cfg(guild_id)["punishments"].get(module) or MODULES[module][3]
        return value if value in PUNISHMENTS else "ban"

    def _wl_keys(self, module: str) -> list[str]:
        """Every whitelist bucket that applies to `module`.

        A bucket is stored under exactly the key it was written with: "all", a
        category name, or a protection name. Inheritance is handled by
        `whitelisted`, so a category is one bucket rather than a copy of itself
        written into each of its protections.
        """
        return ["all"] if module == "all" else [module]

    def wl_get(self, guild_id: int, module: str, kind: str) -> list[int]:
        settings = self.guild_settings(guild_id)
        found: list[int] = []
        for key in self._wl_keys(module):
            if key == "all":
                values = settings.get("antinuke_whitelist" if kind == "users" else "antinuke_whitelist_roles", [])
            else:
                values = settings.get("whitelists", {}).get(key, {}).get(kind, [])
            for value in values:
                if value not in found:
                    found.append(value)
        return found

    def wl_set(self, guild_id: int, module: str, kind: str, values: list[int]) -> None:
        settings = self.guild_settings(guild_id)
        if module == "all":
            settings["antinuke_whitelist" if kind == "users" else "antinuke_whitelist_roles"] = values
        else:
            settings.setdefault("whitelists", {}).setdefault(module, {})[kind] = values
        self.invalidate_whitelist_cache(guild_id)

    def is_protected(self, guild: discord.Guild, user_id: int) -> bool:
        """Server owner, the bot itself, and configured security admins (secadmin role + Administrator) can never be punished."""
        if user_id == guild.owner_id:
            return True
        if self.bot.user is not None and user_id == self.bot.user.id:
            return True
        member = guild.get_member(user_id)
        if member is not None and member.guild_permissions.administrator and self.is_security_admin(guild, member):
            return True
        return False

    def is_trusted_security_admin(self, guild: discord.Guild, member: discord.Member) -> bool:
        """True when the member holds the secadmin role AND has actual Administrator permission."""
        if not member.guild_permissions.administrator:
            return False
        return self.is_security_admin(guild, member)

    def is_security_admin(self, guild: discord.Guild, member: discord.Member) -> bool:
        """True when the member holds the monitored security-admin role."""
        role_id = self.guild_settings(guild.id).get("security_admin_role_id")
        if not role_id:
            return False
        return any(role.id == role_id for role in member.roles)

    def whitelisted(
        self,
        guild: discord.Guild,
        user_id: int,
        module: str,
        member: Optional[discord.Member] = None,
        role_ids: Optional[set[int]] = None,
    ) -> bool:
        """True when the user/role is whitelisted for this protection, its category, or all protections.

        `role_ids` lets a caller that checks several modules against the same member
        build the role set once instead of once per module.
        """
        if role_ids is None:
            member = member or guild.get_member(user_id)
            role_ids = {role.id for role in member.roles} if member is not None else set()
        users, roles = self._whitelist_sets(guild.id, module)
        if user_id in users or role_ids & roles:
            return True
        category = rules_mod.PROTECTIONS[module].category if module in rules_mod.PROTECTIONS else None
        if category:
            users, roles = self._whitelist_sets(guild.id, category)
            if user_id in users or role_ids & roles:
                return True
        users, roles = self._whitelist_sets(guild.id, "all")
        return user_id in users or bool(role_ids & roles)

    def _whitelist_sets(self, guild_id: int, key: str) -> tuple[set[int], set[int]]:
        """Cached set form of one whitelist bucket.

        `wl_get` rebuilds a de-duplicated list on every call, and the chat filters
        call it several times per message per module. The cache is dropped whenever
        the whitelist is written, so it can never go stale in a way that matters.
        """
        cache = self._wl_cache.get(guild_id)
        if cache is None:
            cache = self._wl_cache[guild_id] = {}
        entry = cache.get(key)
        if entry is None:
            entry = (
                set(self.wl_get(guild_id, key, "users")),
                set(self.wl_get(guild_id, key, "roles")),
            )
            cache[key] = entry
        return entry

    def invalidate_whitelist_cache(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._wl_cache.clear()
        else:
            self._wl_cache.pop(guild_id, None)

    # --------------------------------------------------- role grant options
    def role_protection(self, guild_id: int) -> dict[str, Any]:
        """Merged role_protection config for one guild."""
        stored = self.guild_settings(guild_id).get("role_protection")
        merged = dict(ROLE_PROTECTION_DEFAULTS)
        if isinstance(stored, dict):
            merged.update({key: stored[key] for key in merged if key in stored})
        return merged

    def role_dangerous_permissions(self, guild_id: int) -> tuple[str, ...]:
        configured = self.role_protection(guild_id)["dangerous_permissions"]
        keys: list[str] = []
        for item in configured if isinstance(configured, (list, tuple)) else ():
            key = str(item).strip().lower()
            if key in rules_mod.DANGEROUS_PERMISSIONS and key not in keys:
                keys.append(key)
        return tuple(keys) or rules_mod.ROLE_DANGEROUS_PERMISSIONS

    def self_assignable_role_ids(self, guild_id: int) -> set[int]:
        configured = self.role_protection(guild_id)["self_assignable_roles"]
        ids: set[int] = set()
        for item in configured if isinstance(configured, (list, tuple)) else ():
            try:
                ids.add(int(item))
            except (TypeError, ValueError):
                continue
        return ids

    def role_executor_trusted(self, guild: discord.Guild, user_id: int) -> bool:
        """Whitelist check for role grants: explicit user IDs only.

        A whitelisted *role* must not exempt anybody here, otherwise
        whitelisting the "Admin" role would let whoever holds it grant
        dangerous roles untouched.
        """
        if not self.role_protection(guild.id)["whitelist_strict"]:
            return self.whitelisted(guild, user_id, "antirolepermedit")
        if user_id in self._whitelist_sets(guild.id, "antirolepermedit")[0]:
            return True
        return user_id in self._whitelist_sets(guild.id, "all")[0]

    def nuke_active(self, guild: discord.Guild) -> bool:
        return any(self.enabled(guild.id, key) for key in NUKE_MODULES)

    def automod_on(self, guild: discord.Guild) -> bool:
        return any(self.enabled(guild.id, key) for key in CHAT_MODULES)

    def sync_master_flag(self, guild_id: int) -> None:
        modules = self.cfg(guild_id)["modules"]
        self.guild_settings(guild_id)["antinuke_enabled"] = any(modules.get(key) for key in NUKE_MODULES)

    # ---------------------------------------------------------------- helpers
    def permission_warnings(self, guild: discord.Guild) -> list[str]:
        me = guild.me
        if me is None:
            return []
        perms = me.guild_permissions
        needed = {
            "Ban Members": perms.ban_members,
            "Kick Members": perms.kick_members,
            "Moderate Members": perms.moderate_members,
            "Manage Channels": perms.manage_channels,
            "Manage Roles": perms.manage_roles,
            "Manage Webhooks": perms.manage_webhooks,
            "Manage Messages": perms.manage_messages,
            "View Audit Log": perms.view_audit_log,
        }
        if perms.administrator:
            needed = {}
        if not perms.manage_guild and (self.enabled(guild.id, "antiserver") or self.enabled(guild.id, "antiintegration")):
            needed["Manage Guild"] = False
        warnings = []
        missing = [name for name, ok in needed.items() if not ok]
        if missing:
            warnings.append("Missing permissions: " + ", ".join(missing))
        if guild.roles and me.top_role != guild.roles[-1]:
            warnings.append("Move my role to the **top** of the role list, otherwise I cannot punish higher roles.")
        return warnings

    async def write_log(self, guild: discord.Guild, emb: discord.Embed, dm_owner: bool = False) -> None:
        logger = self.bot.get_cog("LoggingCog")
        if logger is not None:
            await cast(Any, logger).write_log(guild, emb)
        if dm_owner and guild.owner is not None:
            try:
                owner_embed = discord.Embed.from_dict(emb.to_dict())
                emojis = moderation_emojis_for(self.bot, guild.id)
                animated_note = (
                    f"{emojis['ARROW_RED']} **Security notice**\n"
                    f"> {emojis['CHEVRONS_RED']} Review the protection event above."
                )
                owner_embed.description = "\n".join(
                    part for part in (owner_embed.description, animated_note) if part
                )
                await guild.owner.send(embed=owner_embed)
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Security event owner DM failed for guild {guild.id}: {type(error).__name__}: {error}")

    def can_punish(self, guild: discord.Guild, member: Optional[discord.Member], action: str) -> Optional[str]:
        me = guild.me
        if me is None:
            return "The bot is not ready yet."
        perms = me.guild_permissions
        allowed = {"ban": perms.ban_members, "kick": perms.kick_members, "timeout": perms.moderate_members}
        if not (perms.administrator or allowed[action]):
            return f"I am missing the permission needed to {action} members."
        if member is not None:
            if member.id == guild.owner_id:
                return "The server owner cannot be punished."
            if member.top_role >= me.top_role:
                return "This member's role is higher than mine. Move my role to the top."
            if action == "timeout" and member.guild_permissions.administrator:
                return "Discord does not allow timing out administrators. Choose Ban or Kick for this protection."
        return None

    async def dm_punished(
        self, guild: discord.Guild, user_id: int, action: str, reason: str, module: str, duration: Optional[str] = None
    ) -> bool:
        """Tell the member why they were punished. Must run BEFORE a ban/kick, otherwise Discord blocks the DM."""
        user = guild.get_member(user_id) or self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException as error:
                print(f"Could not fetch user {user_id} for punishment DM: {type(error).__name__}: {error}")
                return False
        if user.bot:
            return False
        verbs = {"ban": "banned", "kick": "kicked", "timeout": "timed out"}
        emojis = moderation_emojis_for(self.bot, guild.id)
        bot_avatar_url = self.bot.user.display_avatar.url if self.bot.user else None
        emb = build_moderation_action_dm(
            guild_name=guild.name,
            action=verbs[action],
            reason=reason,
            moderator="the automatic protection system",
            emojis=emojis,
            bot_avatar_url=bot_avatar_url,
        )
        arrow = emojis["ARROW_RED"]
        emb.add_field(name=f"{arrow} Server", value=guild.name, inline=True)
        emb.add_field(name=f"{arrow} Punishment", value=PUNISHMENTS[action] + (f" · {duration}" if duration else ""), inline=True)
        emb.add_field(name=f"{arrow} Protection", value=MODULES[module][0], inline=True)
        if guild.icon is not None:
            emb.set_thumbnail(url=guild.icon.url)
        try:
            await user.send(embed=emb)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Punishment DM could not be sent to {user_id}: {type(error).__name__}: {error}")
            return False
        return True

    async def _punish_async(self, guild: discord.Guild, member: discord.Member, module: str, reason: str,
                            log_title: str, log_module: str, delete_seconds: int = 0) -> None:
        """Run punishment + logging as a background task so on_message returns instantly."""
        ok, action, problem = await self.punish(guild, member.id, module, reason, delete_seconds=delete_seconds)
        member_display = member.mention if member.guild else f"<@{member.id}>"
        desc = (
            f"**Offender:** {member_display} (`{member.id}`)\n"
            f"**Reason:** {reason}\n"
            f"**Punishment:** {PUNISHMENTS[action]} — {'applied' if ok else 'FAILED'}"
        )
        if problem:
            desc += f"\n**Reason:** {problem}"
        self._spawn(self.write_log(guild, ui.error(log_title, desc), dm_owner=True), "log-owner-dm")

    async def _purge_and_punish(self, guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable,
                                module: str, reason: str) -> None:
        """Purge 15 msgs from spammer, then punish and log — all async."""
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.purge(limit=15, check=lambda m: m.author.id == member.id)
            except (discord.Forbidden, discord.HTTPException):
                pass
        ok, action, problem = await self.punish(guild, member.id, module, reason)
        desc = (
            f"**Offender:** {member.mention} (`{member.id}`)\n"
            f"**Reason:** {reason}\n"
            f"**Punishment:** {PUNISHMENTS[action]} — {'applied' if ok else 'FAILED'}"
        )
        if problem:
            desc += f"\n**Reason:** {problem}"
        self._spawn(self.write_log(guild, ui.warn("Spam Filter Triggered", desc + (f"\n**Reason:** {problem}" if problem else ""))), "spam-log")

    async def punish(
        self,
        guild: discord.Guild,
        user_id: int,
        module: str,
        reason: str,
        delete_seconds: int = 0,
    ) -> tuple[bool, str, Optional[str]]:
        """Apply the punishment the admin chose in the panel and DM the member. Returns (ok, action, error)."""
        action = self.punishment(guild.id, module)
        member = guild.get_member(user_id)
        problem = self.can_punish(guild, member, action)
        if problem is not None:
            return False, action, problem
        duration = None
        timeout_minutes = 0
        if action == "timeout":
            timeout_minutes = self.timeout_minutes(guild.id, module)
            if timeout_minutes % (24 * 60) == 0:
                days = timeout_minutes // (24 * 60)
                duration = f"{days} day{'s' if days != 1 else ''}"
            elif timeout_minutes % 60 == 0:
                hours = timeout_minutes // 60
                duration = f"{hours} hour{'s' if hours != 1 else ''}"
            else:
                duration = f"{timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}"
        await self.dm_punished(guild, user_id, action, reason, module, duration)
        try:
            if action == "ban":
                await guild.ban(member or discord.Object(id=user_id), reason=reason, delete_message_seconds=delete_seconds)
            elif action == "kick":
                if member is None:
                    return False, action, "Member is no longer in the server."
                await guild.kick(member, reason=reason)
            else:
                if member is None:
                    return False, action, "Member is no longer in the server."
                await member.timeout(timedelta(minutes=timeout_minutes), reason=reason)
        except discord.Forbidden:
            return False, action, "Missing permission or role hierarchy. Move my role to the top."
        except discord.HTTPException as error:
            return False, action, type(error).__name__
        return True, action, None

    # ---------------------------------------------------------- nuke detection
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        try:
            await self._handle_audit_entry(entry)
        except Exception as error:
            print(f"Antinuke could not handle {entry.action} in {entry.guild.id}: {type(error).__name__}: {error}")

    def _audit_changes(self, entry: discord.AuditLogEntry) -> dict:
        """Return audit-log changes as a plain dict using discord.py 2.7.1 before/after attributes."""
        result: dict = {}
        before = getattr(entry, "before", None)
        after = getattr(entry, "after", None)
        if before is not None and after is not None:
            for attr in ("name", "type", "id", "permissions", "position", "color", "hoist", "mentionable", "parent_id", "permission_overwrites", "allow", "deny", "target_type"):
                old_val = getattr(before, attr, None)
                new_val = getattr(after, attr, None)
                if old_val != new_val:
                    result[attr] = new_val
                    result[f"{attr}_before"] = old_val
        return result

    async def _handle_audit_entry(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        user_id = entry.user_id
        if user_id is None:
            return

        # Member role grants get their own analysis: Discord
        # attributes this entry to the member for boosts, bot roles,
        # integrations and onboarding role-selection, so it must never
        # be punished as an instant nuke without checking what the
        # roles actually contain.
        if entry.action == A.member_role_update:
            await self._handle_member_role_update(entry)
            return

        detection = rules_mod.classify(entry)
        module = detection.module if detection is not None else ACTION_MODULE.get(entry.action)
        if module is None or module not in MODULES or not self.enabled(guild.id, module):
            return

        # Server owner and configured security admins (secadmin role + Administrator)
        # are always trusted, even for instant nuke protections.
        member = guild.get_member(user_id)
        is_owner = user_id == guild.owner_id
        is_security_admin = (
            member is not None
            and member.guild_permissions.administrator
            and self.is_security_admin(guild, member)
        )
        if user_id in self.global_protected_owner_ids:
            await self.write_log(
                guild,
                ui.info(
                    "Security Alert \u00b7 Skipped",
                    f"**Protection:** {MODULES[module][0]}\n"
                    f"**Executor:** <@{user_id}> (`{user_id}`)\n"
                    f"**Status:** Skipped \u2014 globally protected bot owner",
                ),
                dm_owner=True,
            )
            return
        if is_owner or is_security_admin:
            return

        # A member who took the server over (deleted the owner's role, became
        # owner) must still be answerable for the destruction they caused.
        if self.is_protected(guild, user_id):
            if not rules_mod.PROTECTIONS[module].instant:
                return
            # Instant nuke protections (delete channel, delete role, guild
            # update, bot add, webhook, integration) bypass the owner exemption:
            # the owner is the most likely person to have been replaced.

        # Tier gating only applies to non-instant protections. An instant
        # nuke action (channel delete, role delete, guild update, ...) must be
        # punished on the first attempt regardless of the plan, because by the
        # time a Plus-tier threshold would be reached the server is already gone.
        if not rules_mod.PROTECTIONS[module].instant and not self.has_tier(guild.id, "prime"):
            return

        # Whitelisted members are NOT fully trusted: they get a higher bar, but
        # handing over control is still punished.
        trusted = self.whitelisted(guild, user_id, module)

        if detection is None:
            # Fall back to the original single-module behaviour for anything the
            # registry does not classify.
            detection = rules_mod.Detection(
                module=module,
                severity=rules_mod.PROTECTIONS[module].severity,
                reason=f"{entry.action.name.replace('_', ' ')} is not allowed here.",
                instant=rules_mod.PROTECTIONS[module].instant,
            )

        await self._process_detection(guild, entry, detection, trusted=trusted)

    async def _process_detection(
        self,
        guild: discord.Guild,
        entry: discord.AuditLogEntry,
        detection: rules_mod.Detection,
        *,
        trusted: bool,
    ) -> None:
        """Shared tail: dedupe, score, then warn / revert / punish."""
        # Audit entries can be delivered twice after a gateway reconnect. The
        # engine drops the repeat, and this in-flight guard stops two coroutines
        # from punishing the same actor for one entry.
        if not self.risk.mark_entry(guild.id, entry.id):
            return
        user_id = entry.user_id
        if user_id is None:
            return
        guard = (guild.id, entry.id or 0)
        if guard in self._handling:
            return

        try:
            self._handling.add(guard)
            verdict = self.risk.evaluate(
                guild.id,
                user_id,
                detection,
                trusted=trusted,
                lockdown_active=self.lockdowns.is_active(guild.id),
            )
        finally:
            self._handling.discard(guard)

        if verdict.action == "warn":
            await self._forward_antinuke_alert(guild, entry, detection, "warned (threshold not reached)", trusted)
            await self._warn_threshold(guild, entry, detection, verdict)
            return
        if not verdict.punish:
            await self._forward_antinuke_alert(guild, entry, detection, verdict.action or "logged", trusted)
            # Still revert when the action is reversible: an allowed threshold
            # crossing must not leave a deleted channel behind.
            if verdict.action == "allow" and user_id is not None and self.risk.score_of(guild.id, user_id) >= risk_mod.LOCKDOWN_SCORE * 0.8:
                self._spawn(self.revert(guild, entry, detection), "revert-allowed")
            return

        await self._forward_antinuke_alert(guild, entry, detection, "punished", trusted)
        self._spawn(
            self._respond_to_detection(guild, entry, detection, verdict, trusted=trusted),
            f"respond:{detection.module}",
        )

    async def _forward_antinuke_alert(self, guild, entry, detection, action_taken, trusted) -> None:
        key = (guild.id, getattr(entry, "id", 0) or 0)
        if key in self._forwarded_alerts:
            return
        self._forwarded_alerts.add(key)
        hub = self.bot.get_cog("OwnerHub")
        if hub is None:
            return
        ch_id = None
        ids = getattr(hub, "channel_ids", {}) or {}
        ch_id = ids.get("anti-nuke-alerts")
        if not ch_id:
            ch_id = getattr(hub, "anti_nuke_channel_id", None)
        if not ch_id:
            return
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ch_id)
            except Exception:
                return
        try:
            target = getattr(entry, "target", None)
            target_name = getattr(target, "name", None) or getattr(target, "display_name", None) or str(getattr(target, "id", ""))
            target_id = getattr(target, "id", None)
            executor = guild.get_member(entry.user_id) if entry.user_id is not None else None
            executor_name = executor.display_name if executor is not None else str(entry.user_id)
            embed = discord.Embed(title="Anti-Nuke Alert", color=0xED4245, timestamp=discord.utils.utcnow())
            embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
            embed.add_field(name="Action", value=str(getattr(entry.action, "name", entry.action)), inline=True)
            embed.add_field(name="Protection", value=MODULES.get(detection.module, (detection.module,))[0], inline=True)
            embed.add_field(name="Executor", value=f"{executor_name} (`{entry.user_id}`)", inline=False)
            embed.add_field(name="Target", value=f"{target_name} (`{target_id}`)", inline=False)
            embed.add_field(name="Action Taken", value=str(action_taken), inline=True)
            embed.add_field(name="Trusted", value=str(bool(trusted)), inline=True)
            embed.add_field(name="Reason", value=str(detection.reason), inline=False)
            await channel.send(embed=embed)
        except Exception:
            pass

    async def _warn_threshold(self, guild, entry, detection, verdict) -> None:
        await self.write_log(
            guild,
            ui.warn(
                "Protection Warning",
                f"**Protection:** {MODULES[detection.module][0]}\n"
                f"**Attempted by:** <@{entry.user_id}> (`{entry.user_id}`)\n"
                f"**Detection:** {detection.reason}\n"
                f"**Count:** {verdict.count}/{verdict.limit} actions within the limit\n"
                f"**Risk score:** {verdict.risk_score:.0f} ({verdict.severity})\n"
                f"**Next:** {verdict.reason}",
            ),
        )

    # ------------------------------------------------- member role grants
    async def _handle_member_role_update(self, entry: discord.AuditLogEntry) -> None:
        """Anti Role Permission Edit, specialised for role grants.

        `member_role_update` is how Discord records *every* role change
        on a member, and it attributes system-driven changes (server
        boosts, bot roles, integrations, onboarding role-selection) to
        the member themself. Treating the entry as an instant nuke
        banned legit members, so the actual roles are resolved and
        classified first: only changes that hand over power are
        punished instantly, everything else escalates through
        log -> warn -> punish.
        """
        if entry.user_id is not None and entry.user_id in self.global_protected_owner_ids:
            await self.write_log(
                entry.guild,
                ui.info(
                    "Security Alert \u00b7 Skipped",
                    f"**Protection:** Anti Role Permission Edit\n"
                    f"**Executor:** <@{entry.user_id}> (`{entry.user_id}`)\n"
                    f"**Status:** Skipped \u2014 globally protected bot owner",
                ),
                dm_owner=True,
            )
            return
        guild = entry.guild
        user_id = entry.user_id
        target_id = getattr(entry.target, "id", None)
        if user_id is None or target_id is None:
            return
        if not self.enabled(guild.id, "antirolepermedit"):
            return

        # The owner can manage roles, but bot changes still pass the
        # dangerous-permission check below.
        if user_id == guild.owner_id:
            return
        # Boosts and integration-driven changes are attributed to the
        # member themself; there is no human actor to punish.
        if rules_mod.integration_driven(entry):
            return

        # A replayed entry from before a restart carries no new
        # information, so only fresh entries are analysed.
        max_age = float(self.role_protection(guild.id)["audit_max_age"])
        if (discord.utils.utcnow() - entry.created_at).total_seconds() > max_age:
            return

        added, removed = rules_mod.member_role_change(entry)
        if not added and not removed:
            # Gateway-pushed entries can arrive without the role diff.
            # Wait briefly and re-read the same entry through the audit
            # log API, which always includes the before/after lists.
            added, removed = await self._refetch_role_change(guild, entry)
        if not added and not removed:
            # Still unverifiable: punishing needs to know *which* roles
            # changed, so no ban here — the staff gets an alert instead.
            await self._role_change_unverified(guild, entry)
            return

        added = await self._resolve_roles(guild, added)
        removed = await self._resolve_roles(guild, removed)

        options = self.role_protection(guild.id)
        self_assignable_ids = self.self_assignable_role_ids(guild.id)
        detection = rules_mod.classify_member_role_update(
            entry,
            added=added,
            removed=removed,
            dangerous_keys=self.role_dangerous_permissions(guild.id),
            self_assignable_roles=self_assignable_ids,
            ignore_onboarding_roles=bool(options["ignore_onboarding_roles"]),
        )
        if detection is None:
            return

        if detection.instant:
            # A granted dangerous permission is a privilege escalation:
            # process it immediately, even for whitelisted actors.
            await self._process_detection(
                guild,
                entry,
                detection,
                trusted=self.role_executor_trusted(guild, user_id),
            )
            return

        # Non-dangerous changes are graduated, so the tier gate that
        # normally sits in _handle_audit_entry applies here.
        if not self.has_tier(guild.id, "prime"):
            return

        executor = guild.get_member(user_id)
        is_trusted_role_bot = (
            (
                executor is not None
                and executor.bot
                and executor.id in self.trusted_role_management_bot_ids
            )
            or (
                self.bot.user is not None
                and user_id == self.bot.user.id
            )
        )
        is_self_role_change = user_id == target_id and (executor is None or not executor.bot)
        # A bot assigning a self-assignable role (e.g., Dyno self-roles) is
        # treated as legitimate: the role was configured as self-assignable
        # by an administrator, so any bot performing the assignment is allowed
        # for non-dangerous roles. Dangerous permission grants are already
        # handled above and punished regardless of actor.
        self_assignable = self_assignable_ids
        is_bot_self_role = (
            executor is not None
            and executor.bot
            and bool(self_assignable)
            and {getattr(r, "id", 0) for r in added + removed}.issubset(
                {int(rid) for rid in self_assignable}
            )
        )
        if is_trusted_role_bot or is_self_role_change or is_bot_self_role:
            actor_label = (
                f"<@{user_id}> (`{user_id}`), configured role-management bot"
                if is_trusted_role_bot
                else f"<@{user_id}> (`{user_id}`), bot self-role assignment"
                if is_bot_self_role
                else f"<@{user_id}> (`{user_id}`), member self-role selection"
            )
            desc = (
                f"**Event:** {entry.action.name.replace('_', ' ')}\n"
                f"**Actor:** {actor_label}\n"
                f"**Affected member:** <@{target_id}> (`{target_id}`)\n"
                f"**Added:** {' '.join(role.mention for role in added) or 'none'}\n"
                f"**Removed:** {' '.join(role.mention for role in removed) or 'none'}\n"
                "**Decision:** allowed; no dangerous permissions were granted."
            )
            await self.write_log(guild, ui.info("Non-Dangerous Role Change Allowed", desc))
            return
        if self.role_executor_trusted(guild, user_id):
            # Explicitly whitelisted users may hand out normal roles.
            return

        # Every graduated event is logged; the risk engine warns at
        # grant_punish_at - 1 and applies the punishment at
        # grant_punish_at, including stripping the granted roles.
        desc = (
            f"**Member:** <@{target_id}> (`{target_id}`)\n"
            f"**By:** <@{user_id}> (`{user_id}`)\n"
            f"**Added:** {' '.join(role.mention for role in added) or 'none'}\n"
            f"**Removed:** {' '.join(role.mention for role in removed) or 'none'}\n"
            f"**Escalation:** {int(options['grant_punish_at'])} non-dangerous role changes "
            f"within {int(options['grant_window'])}s lead to a role strip and the "
            f"protection's punishment "
            f"({PUNISHMENTS[self.punishment(guild.id, 'antirolepermedit')]})."
        )
        await self.write_log(guild, ui.info("Role Change Logged", desc))

        detection.limit = int(options["grant_punish_at"])
        detection.window = float(options["grant_window"])
        await self._process_detection(guild, entry, detection, trusted=False)

    async def _refetch_role_change(
        self, guild: discord.Guild, entry: discord.AuditLogEntry
    ) -> tuple[list[Any], list[Any]]:
        """Re-read one audit entry through the API after a short delay.

        Entries pushed over the gateway sometimes arrive without the
        change list. Waiting a moment gives Discord time to persist the
        full entry, and reading the audit log then returns the complete
        before/after role lists.
        """
        delay = float(self.role_protection(guild.id)["audit_retry_delay"])
        await asyncio.sleep(delay)
        try:
            async for audit_entry in guild.audit_logs(action=A.member_role_update, limit=50):
                if audit_entry.id != entry.id:
                    continue
                return rules_mod.member_role_change(audit_entry)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        return [], []

    async def _resolve_roles(self, guild: discord.Guild, roles: list[Any]) -> list[discord.Role]:
        """Turn audit-log role references into full Role objects.

        Audit diffs can contain bare objects without permissions when a
        role is not cached, and the permissions decide whether a grant
        is dangerous. Uncached roles are therefore fetched once, capped
        so a flood of entries cannot turn into an API hammer.
        """
        resolved: list[discord.Role] = []
        fetched = 0
        for role in roles:
            role_id = getattr(role, "id", None)
            if role_id is None:
                continue
            cached = guild.get_role(role_id)
            if cached is not None:
                resolved.append(cached)
                continue
            if fetched >= ROLE_RESOLVE_FETCH_LIMIT:
                continue
            fetched += 1
            try:
                resolved.append(await guild.fetch_role(role_id))
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                # A role that cannot be read cannot be granted
                # meaningfully any more; dropping it is the safe choice.
                continue
        return resolved

    async def _role_change_unverified(self, guild: discord.Guild, entry: discord.AuditLogEntry) -> None:
        """Report a role change whose diff could not be verified.

        Without the diff there is no proof of what happened and nothing
        to revert, so the change is reported to the log channel instead
        of punished.
        """
        target_id = getattr(entry.target, "id", None)
        delay = self.role_protection(guild.id)["audit_retry_delay"]
        desc = (
            f"**Member:** <@{target_id}> (`{target_id}`)\n"
            f"**Recorded executor:** <@{entry.user_id}> (`{entry.user_id}`)\n"
            f"**Action:** {entry.action.name.replace('_', ' ')}\n\n"
            f"The audit log entry had no role diff even after **{delay}s** retry, "
            "so **no punishment was applied** and the change could not be "
            "reverted. Check the member's current roles manually."
        )
        await self.write_log(guild, ui.warn("Role Change Unverified", desc))

    async def _respond_to_detection(self, guild, entry, detection, verdict, *, trusted: bool = False) -> None:
        """Punish, revert and log one confirmed detection."""
        reason = f"{detection.reason} ({MODULES[detection.module][0]} protection)"
        if trusted:
            # Whitelisted actors are exempt from enforcement: no punishment,
            # no destructive restoration, and no "punishment applied" alert.
            ok, action, problem = True, "none", None
            restored = ""
            self.security_log[guild.id].append(
                {
                    "executor": entry.user_id,
                    "protection": detection.module,
                    "severity": verdict.severity,
                    "risk": round(verdict.risk_score, 1),
                    "action": entry.action.name,
                    "target": str(getattr(entry.target, "id", "")),
                    "reason": detection.reason,
                    "punishment": "skipped",
                    "applied": False,
                    "restored": restored,
                    "trusted": trusted,
                }
            )
            label = MODULES[detection.module][0]
            desc = (
                f"**Severity:** {verdict.severity}\n"
                f"**Protection:** {label}\n"
                f"**Executor:** <@{entry.user_id}> (`{entry.user_id}`)\n"
                f"**Action:** {entry.action.name.replace('_', ' ')}\n"
                f"**Target:** {self._target_label(entry)}\n"
                f"**Detection:** {detection.reason}\n"
                f"**Status:** Skipped \u2014 executor whitelisted"
            )
            await self.write_log(guild, ui.info("Security Alert \u00b7 Skipped", desc), dm_owner=True)
            return

        if verdict.emergency:
            await self._auto_lockdown(guild, entry, verdict)

        self.security_log[guild.id].append(
            {
                "executor": entry.user_id,
                "protection": detection.module,
                "severity": verdict.severity,
                "risk": round(verdict.risk_score, 1),
                "action": entry.action.name,
                "target": str(getattr(entry.target, "id", "")),
                "reason": detection.reason,
                "punishment": PUNISHMENTS[action],
                "applied": ok,
                "restored": restored,
                "trusted": trusted,
            }
        )

        label = MODULES[detection.module][0]
        desc = (
            f"**Severity:** {verdict.severity}\n"
            f"**Protection:** {label}\n"
            f"**Executor:** <@{entry.user_id}> (`{entry.user_id}`)\n"
            f"**Action:** {entry.action.name.replace('_', ' ')}\n"
            f"**Target:** {self._target_label(entry)}\n"
            f"**Detection:** {detection.reason}\n"
            f"**Risk score:** {verdict.risk_score:.0f} · {verdict.count}/{verdict.limit} in this window"
            f" · {verdict.burst_count} total in {int(risk_mod.BURST_WINDOW)}s\n"
            f"**Punishment:** {PUNISHMENTS[action]} — {'applied' if ok else 'FAILED'}"
        )
        if trusted:
            desc += "\n**Note:** this actor is whitelisted for this protection."
        if restored:
            desc += f"\n**Restoration:** {restored}"
        if problem:
            desc += f"\n**Problem:** {problem}"
        for note in detection.notes:
            desc += f"\n**Signal:** {note}"
        emb = (
            ui.error("Security Alert · " + verdict.severity, desc)
            if ok
            else ui.warn("Security Alert · Punishment Failed", desc)
        )
        await self.write_log(guild, emb, dm_owner=True)

    @staticmethod
    def _target_label(entry) -> str:
        target = getattr(entry, "target", None)
        if target is None:
            return "unknown"
        name = getattr(target, "name", None) or getattr(target, "id", None)
        return f"{name} (`{getattr(target, 'id', '?')}`)"

    async def _auto_lockdown(self, guild, entry, verdict) -> None:
        """Lock the server automatically when the pattern looks like a real attack."""
        result = await self.lockdowns.engage(
            guild,
            reason=f"{verdict.severity} {verdict.reason}",
            triggered_by=f"{entry.user_id} via {entry.action.name.replace('_', ' ')}",
            duration=lockdown_mod.DEFAULT_DURATION,
        )
        if result is None or not result.engaged:
            return
        desc = (
            f"**Triggered by:** <@{entry.user_id}> (`{entry.user_id}`)\n"
            f"**Reason:** {verdict.reason}\n"
            f"**Risk score:** {verdict.risk_score:.0f}\n"
            f"**Channels locked:** {len(result.state.touched)}\n"
            f"**Duration:** {lockdown_mod.DEFAULT_DURATION // 60} minutes\n"
            f"**Staff channels kept open:** "
            f"{', '.join(f'<#{cid}>' for cid in lockdown_mod.LockdownManager.safe_channel_ids(guild)[:5]) or 'none found'}"
        )
        desc += "\n" + "\n".join(f"**Note:** {note}" for note in result.notes)
        await self.write_log(guild, ui.error("LOCKDOWN ENGAGED", desc), dm_owner=True)

    async def revert(self, guild: discord.Guild, entry: discord.AuditLogEntry, detection: Any = None) -> str:
        """Undo an unauthorised action as far as Discord allows.

        `detection` is optional so the old call signature keeps working. When it
        is supplied its classification drives the undo, which is what makes the
        new protections able to restore state rather than only punish.
        """
        reason = "Security: reverting unauthorised action"
        action = entry.action
        module = detection.module if detection is not None else ACTION_MODULE.get(action)
        try:
            if action == A.guild_update:
                return await self.restore_guild(guild, entry, reason)
            if action == A.integration_create:
                return await self.delete_integration(guild, entry, reason)
            if action in rules_mod.OVERWRITE_ACTIONS:
                return await self.restore_overwrite(guild, entry, reason)
            target_id = getattr(entry.target, "id", None)
            if target_id is None:
                return ""
            if action == A.channel_create:
                channel = guild.get_channel(target_id)
                if channel is None:
                    return ""
                await channel.delete(reason=reason)
                return f"Deleted the channel #{channel.name}."
            if action == A.role_create:
                role = guild.get_role(target_id)
                if role is None:
                    return ""
                await role.delete(reason=reason)
                return f"Deleted the role {role.name}."
            if action == A.webhook_create:
                for webhook in await guild.webhooks():
                    if webhook.id == target_id:
                        await webhook.delete(reason=reason)
                        return f"Deleted the webhook {webhook.name}."
                return ""
            if action == A.bot_add:
                bot_member = guild.get_member(target_id)
                if bot_member is None:
                    return ""
                await guild.kick(bot_member, reason=reason)
                return f"Removed the bot {bot_member}."
            if action == A.ban:
                await guild.unban(discord.Object(id=target_id), reason=reason)
                return "Lifted the ban."
            if action == A.channel_delete:
                return await self.restore_channel(guild, entry, reason)
            if action == A.role_delete:
                return await self.restore_role(guild, entry, reason)
            if action in (A.channel_update, A.role_update):
                return await self.restore_update(guild, entry, reason)
            if action == A.member_role_update:
                return await self.revert_member_roles(guild, entry, detection, reason)
            return await self.revert_new_protection(guild, entry, reason, module)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError, AttributeError, KeyError) as error:
            return f"Could not undo the change ({type(error).__name__}: {error})."

    async def revert_member_roles(
        self,
        guild: discord.Guild,
        entry: discord.AuditLogEntry,
        detection: Any,
        reason: str,
    ) -> str:
        """Undo a member role change: strip granted roles, restore removed ones."""
        details = getattr(detection, "details", None) or {}
        added_ids = [self._as_int(role_id) for role_id in details.get("added_role_ids") or []]
        removed_ids = [self._as_int(role_id) for role_id in details.get("removed_role_ids") or []]
        target_id = getattr(entry.target, "id", None)
        member = guild.get_member(target_id) if target_id is not None else None
        if member is None:
            return "The member left the server, so the role change could not be reverted."
        added = [
            role for role in (guild.get_role(role_id) for role_id in added_ids if role_id)
            if role is not None
        ]
        removed = [
            role for role in (guild.get_role(role_id) for role_id in removed_ids if role_id)
            if role is not None
        ]
        if not added and not removed:
            return ""
        try:
            if added:
                await member.remove_roles(*added, reason=reason)
            if removed:
                await member.add_roles(*removed, reason=reason)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
            return f"Could not revert the role change ({type(error).__name__}: {error})."
        parts = []
        if added:
            parts.append("stripped " + ", ".join(f"**{role.name}**" for role in added))
        if removed:
            parts.append("restored " + ", ".join(f"**{role.name}**" for role in removed))
        return "Reverted the role change: " + "; ".join(parts) + "."

    async def revert_new_protection(self, guild, entry, reason: str, module: Optional[str]) -> str:
        """Undo the actions the original bot had no handler for.

        Discord simply does not expose an API for some of these, and saying so
        plainly in the log is better than reporting a rollback that never
        happened.
        """
        action = entry.action
        target_id = getattr(entry.target, "id", None)

        if action == A.invite_create and target_id is not None:
            # Only revoke invites this bot can still see; a used or expired
            # invite is already harmless.
            invite = await self._find_invite(guild, target_id)
            if invite is None:
                return "The invite had already been used or expired."
            try:
                await invite.delete(reason=reason)
                return f"Revoked the invite `{invite.code}`."
            except discord.Forbidden:
                return "I could not revoke the invite (missing Manage Server)."
            except discord.HTTPException as error:
                return f"Could not revoke the invite ({type(error).__name__})."

        if action in (A.emoji_delete, A.emoji_create, A.emoji_update, A.sticker_delete, A.sticker_create, A.sticker_update):
            return self._asset_note(action, entry)

        if action in (A.soundboard_sound_create, A.soundboard_sound_update):
            return self._soundboard_note(entry)

        if action in (A.stage_instance_create, A.stage_instance_update):
            return "Stage configuration changed. Discord does not expose the previous value, so this cannot be restored."

        if action in (A.automod_rule_create, A.automod_rule_delete, A.automod_rule_update):
            return self._automod_note(action, entry)

        if action in (
            A.onboarding_create, A.onboarding_update,
            A.onboarding_prompt_create, A.onboarding_prompt_update, A.onboarding_prompt_delete,
        ):
            return "Onboarding changed. Discord exposes no API to read or restore onboarding, so this is reported only."

        if action == A.thread_delete:
            return f"Thread `{target_id}` was deleted. Discord does not allow a bot to recreate threads."

        return ""

    async def _find_invite(self, guild: discord.Guild, invite_id: int):
        invites = await guild.invites() if hasattr(guild, "invites") else []
        for invite in invites:
            if invite.id == invite_id or invite.code == str(invite_id):
                return invite
        return None

    @staticmethod
    def _asset_note(action, entry) -> str:
        name = getattr(entry.target, "name", "unknown")
        if action in (A.emoji_delete, A.sticker_delete):
            # Discord never returns the original bytes for a deleted asset, so a
            # bot cannot re-upload it. Saying so is better than pretending.
            return f"{name} was deleted. Discord does not allow a bot to recover the file, so it must be re-added."
        if action == A.emoji_create:
            return f"Emoji `{name}` was added. Too many new emojis can hit the server emoji limit."
        if action == A.sticker_create:
            return f"Sticker `{name}` was added. Sticker slots are limited, so mass creation locks out legitimate stickers."
        return f"`{name}` was modified."

    @staticmethod
    def _soundboard_note(entry) -> str:
        name = getattr(entry.target, "name", "unknown")
        return f"Soundboard sound `{name}` was added or changed. Only administrators should manage these."

    @staticmethod
    def _automod_note(action, entry) -> str:
        name = getattr(entry.target, "name", "unknown")
        if action == A.automod_rule_delete:
            return f"AutoMod rule `{name}` was deleted. Turn the remaining rules back on manually if this was not you."
        if action == A.automod_rule_update:
            return f"AutoMod rule `{name}` was changed. Discord does not expose the previous rule, so review it manually."
        return f"AutoMod rule `{name}` was created. Discord does not expose a previous version, so review it manually."

    async def restore_overwrite(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        """Undo an unauthorised channel permission overwrite.

        Discord records what the overwrite became, never what it was before, so
        only a freshly created overwrite can actually be undone. A changed or
        deleted one is surfaced for an admin to review instead of being reported
        as rolled back when it was not.
        """
        channel = entry.target
        if not isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)
        ):
            return ""
        changes: Any = self._audit_changes(entry)
        who = self._as_int(changes.get("id"))
        if entry.action == A.overwrite_create:
            try:
                if who is None:
                    return ""
                target = guild.get_role(who) if self._as_text(changes.get("target_type") or changes.get("type")) == "role" else guild.get_member(who)
                if target is None:
                    return ""
                await channel.set_permissions(target, overwrite=None, reason=reason)
                return f"Removed the new permission overwrite on #{channel.name}."
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
                return f"Could not remove the overwrite on #{channel.name} ({type(error).__name__})."
        kind = "deleted" if entry.action == A.overwrite_delete else "changed"
        return (
            f"A permission overwrite on #{channel.name} was {kind}. Discord does not expose the previous "
            f"overwrite, so review this channel manually."
        )

    # ---------------------------------------------------------- restore helpers
    @staticmethod
    def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_text(value: Any, limit: int = 100) -> str:
        text = str(value or "").strip()
        return text[:limit]

    @classmethod
    def _as_permissions(cls, value: Any) -> Optional[discord.Permissions]:
        raw = cls._as_int(value)
        if raw is None:
            return None
        try:
            return discord.Permissions(raw)
        except ValueError:
            return None

    async def _download(self, url: str) -> Optional[bytes]:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

    @staticmethod
    def _category_of(guild: discord.Guild, overwrites: Any) -> Optional[discord.CategoryChannel]:
        """Audit logs store the parent category as the first overwrite entry."""
        if not isinstance(overwrites, (list, tuple)):
            return None
        for item in overwrites:
            if not isinstance(item, dict) or SecurityCog._as_int(item.get("type"), 1) != 0:
                continue
            channel = guild.get_channel(SecurityCog._as_int(item.get("id"), 0) or 0)
            if isinstance(channel, discord.CategoryChannel):
                return channel
        return None

    async def _apply_overwrites(self, guild: discord.Guild, channel: Any, overwrites: Any, reason: str) -> int:
        if not isinstance(overwrites, (list, tuple)):
            return 0
        applied = 0
        for item in overwrites:
            if not isinstance(item, dict):
                continue
            target_id = self._as_int(item.get("id"))
            if target_id is None:
                continue
            kind = self._as_int(item.get("type"), 0) or 0
            target = guild.get_member(target_id) if kind == 1 else guild.get_role(target_id)
            if target is None:
                continue
            allow = self._as_int(item.get("allow"), 0) or 0
            deny = self._as_int(item.get("deny"), 0) or 0
            try:
                overwrite = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(allow), discord.Permissions(deny)
                )
                await channel.set_permissions(target, overwrite=overwrite, reason=reason)
                applied += 1
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                continue
        return applied

    async def restore_channel(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        before = getattr(entry, "before", None)
        name = self._as_text(getattr(before, "name", None)) or "restored-channel"
        kind = self._as_int(getattr(before, "type", None), 0) or 0
        overwrites = getattr(before, "permission_overwrites", None)
        category = self._category_of(guild, overwrites)
        created = None
        try:
            if kind in (0, 5):
                created = await guild.create_text_channel(
                    name,
                    category=category,
                    news=kind == 5,
                    topic=self._as_text(getattr(before, "topic", None), 1024),
                    nsfw=bool(getattr(before, "nsfw", False)),
                    reason=reason,
                )
            elif kind == 2:
                bitrate = max(8000, min(128000, self._as_int(getattr(before, "bitrate", None), 64000) or 64000))
                user_limit = max(0, min(99, self._as_int(getattr(before, "user_limit", None), 0) or 0))
                created = await guild.create_voice_channel(
                    name, category=category, bitrate=bitrate, user_limit=user_limit, reason=reason
                )
            elif kind == 4:
                created = await guild.create_category(name, reason=reason)
            elif kind == 13:
                created = await guild.create_stage_channel(name, category=category, reason=reason)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
            return f"Could not restore #{name} ({type(error).__name__})."
        if created is None:
            return ""
        applied = await self._apply_overwrites(guild, created, overwrites, reason)
        note = f"Restored #{name}"
        if applied:
            note += f" with {applied} permission overwrite(s)"
        note += "."
        if category is None:
            note += " It sits at the top level because its category was already gone."
        return note

    async def restore_role(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        before = getattr(entry, "before", None)
        name = self._as_text(getattr(before, "name", None)) or "restored role"
        if any(role.name == name for role in guild.roles):
            return f"A role named **{name}** already exists, so it was not recreated."
        permissions = self._as_permissions(getattr(before, "permissions", None))
        colour = self._as_int(getattr(before, "color", None), 0) or 0
        try:
            role = await guild.create_role(
                name=name,
                permissions=permissions if permissions is not None else discord.Permissions.none(),
                colour=max(0, min(0xFFFFFF, colour)),
                hoist=bool(getattr(before, "hoist", False)),
                mentionable=bool(getattr(before, "mentionable", False)),
                reason=reason,
            )
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
            return f"Could not restore the role **{name}** ({type(error).__name__})."
        position = self._as_int(getattr(before, "position", None))
        if position is not None:
            try:
                await role.edit(position=max(0, position), reason=reason)
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                return f"Restored the role **{name}**, but not its original position."
        return f"Restored the role **{name}** with its colour, permissions and position."

    async def restore_update(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        changes: Any = self._audit_changes(entry)
        target = entry.target
        if not changes:
            return ""
        if entry.action == A.role_update:
            if not isinstance(target, discord.Role):
                return ""
            options: dict[str, Any] = {}
            if "name" in changes:
                options["name"] = self._as_text(changes.get("name_before")) or target.name
            if "permissions" in changes:
                permissions = self._as_permissions(changes.get("permissions_before"))
                if permissions is not None:
                    options["permissions"] = permissions
            for colour_key in ("color", "colour"):
                if colour_key in changes:
                    colour = self._as_int(changes.get(colour_key + "_before"))
                    if colour is not None:
                        options["color"] = max(0, min(0xFFFFFF, colour))
            if "hoist" in changes:
                options["hoist"] = bool(changes.get("hoist_before"))
            if "mentionable" in changes:
                options["mentionable"] = bool(changes.get("mentionable_before"))
            position = self._as_int(changes.get("position_before")) if "position" in changes else None
            if not options and position is None:
                return ""
            try:
                if options:
                    await target.edit(reason=reason, **options)
                if position is not None:
                    await target.edit(position=max(0, position), reason=reason)
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
                return f"Could not restore the role **{target.name}** ({type(error).__name__})."
            return f"Restored the previous settings of **{target.name}**."

        if not isinstance(target, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)):
            return ""
        voice = isinstance(target, (discord.VoiceChannel, discord.StageChannel))
        text = isinstance(target, (discord.TextChannel, discord.StageChannel))
        options = {}
        for key in CHANNEL_SETTINGS:
            if key not in changes:
                continue
            old = changes.get(str(key) + "_before")
            if key == "name":
                options["name"] = self._as_text(old) or target.name
            elif key == "topic" and text:
                options["topic"] = self._as_text(old, 1024)
            elif key == "nsfw" and text:
                options["nsfw"] = bool(old)
            elif key == "rate_limit_per_user" and text:
                options["slowmode_delay"] = max(0, min(21600, self._as_int(old, 0) or 0))
            elif key == "bitrate" and voice:
                options["bitrate"] = max(8000, min(128000, self._as_int(old, 64000) or 64000))
            elif key == "user_limit" and voice:
                options["user_limit"] = max(0, min(99, self._as_int(old, 0) or 0))
            elif key == "rtc_region" and voice:
                options["rtc_region"] = self._as_text(old, 20) or None
        if "parent_id" in changes and not isinstance(target, discord.CategoryChannel):
            parent_id = self._as_int(changes.get("parent_id_before"))
            parent = guild.get_channel(parent_id) if parent_id else None
            options["category"] = parent if isinstance(parent, discord.CategoryChannel) else None
        if not options:
            return ""
        try:
            await target.edit(reason=reason, **options)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
            return f"Could not restore #{target.name} ({type(error).__name__})."
        note = f"Restored the previous settings of #{target.name}."
        if "permission_overwrites" in changes:
            applied = await self._apply_overwrites(guild, target, changes.get("permission_overwrites_before"), reason)
            if applied:
                note += f" Re-applied {applied} permission overwrite(s)."
        return note

    async def restore_guild(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        changes: Any = self._audit_changes(entry)
        if not changes:
            return ""
        options: dict[str, Any] = {}
        for key, (attribute, caster) in GUILD_TEXT_SETTINGS.items():
            if key not in changes:
                continue
            old = changes.get(str(key) + "_before")
            if old is None:
                continue
            try:
                value = caster(old)
            except (TypeError, ValueError):
                continue
            if attribute == "verification_level":
                value = discord.VerificationLevel(max(0, min(3, int(value))))
            elif attribute == "explicit_content_filter":
                value = discord.ContentFilter(max(0, min(2, int(value))))
            elif attribute == "default_notifications":
                value = discord.NotificationLevel(max(0, min(2, int(value))))
            elif attribute == "name":
                value = self._as_text(value) or guild.name
            elif attribute == "afk_timeout":
                value = max(60, min(36000, int(value)))
            options[attribute] = value
        for channel_key, option in (("afk_channel_id", "afk_channel"), ("system_channel_id", "system_channel")):
            if channel_key not in changes:
                continue
            channel_id = self._as_int(changes.get(channel_key + "_before"))
            channel = guild.get_channel(channel_id) if channel_id else None
            if option == "afk_channel":
                options[option] = channel if isinstance(channel, discord.VoiceChannel) else None
            else:
                options[option] = channel if isinstance(channel, discord.TextChannel) else None
        for key, attribute, folder in GUILD_ASSETS:
            if key not in changes:
                continue
            old = changes.get(str(key) + "_before")
            if not old:
                continue
            data = await self._download(f"https://cdn.discordapp.com/{folder}/{guild.id}/{old}.png")
            if data:
                options[attribute] = data
        if not options:
            return ""
        try:
            await guild.edit(reason=reason, **options)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as error:
            return f"Could not restore the server settings ({type(error).__name__})."
        restored = ", ".join(sorted(options))
        return f"Restored the server settings: {restored}."

    async def delete_integration(self, guild: discord.Guild, entry: discord.AuditLogEntry, reason: str) -> str:
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return ""
        try:
            integrations = await guild.integrations()
        except (discord.Forbidden, discord.HTTPException) as error:
            return f"Could not list integrations ({type(error).__name__})."
        for integration in integrations:
            if integration.id != target_id:
                continue
            name = integration.name
            try:
                await integration.delete(reason=reason)
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                return f"Could not delete the integration **{name}**."
            return f"Deleted the integration **{name}**."
        return ""

    # ----------------------------------------------------------- chat protections
    async def notify(self, channel: discord.abc.Messageable, emb: discord.Embed) -> Optional[discord.Message]:
        try:
            return await channel.send(embed=emb)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"[security] notification could not be sent: {type(error).__name__}: {error}")
            return None

    def _moderation_emojis(self, guild_id: int) -> dict[str, str]:
        bot = getattr(self, "bot", None)
        mass_cog = bot.get_cog("MassMentionCog") if bot is not None else None
        return dict(getattr(mass_cog, "emojis", {}).get(guild_id, {}))

    def _bot_avatar_url(self) -> Optional[str]:
        user = getattr(getattr(self, "bot", None), "user", None)
        return user.display_avatar.url if user is not None else None

    @staticmethod
    async def safe_delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._check_message(message)
        except Exception as error:
            print(f"[security] message check failed: {type(error).__name__}: {error}")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content and before.attachments == after.attachments:
            return
        try:
            await self._check_message(after)
        except Exception as error:
            print(f"[security] edited-message check failed: {type(error).__name__}: {error}")

    async def _check_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return
        if not self.has_tier(guild.id, "prime"):
            return
        if message.author.bot:
            await self._check_bot_raid_message(message)
            return
        if not isinstance(message.author, discord.Member):
            return
        member = message.author
        if member.id in self.global_protected_owner_ids:
            return
        # Cheap first pass: if no chat module is enabled there is nothing to
        # check, so skip building the role set and the whitelist lookups that
        # used to run on every single message in every server.
        enabled_modules = [key for key in CHAT_MODULES if self.enabled(guild.id, key)]
        if not enabled_modules:
            return
        # The role set is identical for every module, so build it once instead of
        # once per CHAT_MODULES entry on every single message.
        role_ids = {role.id for role in member.roles}
        active = {
            key for key in enabled_modules
            if not self.whitelisted(guild, member.id, key, member, role_ids)
        }
        if not active:
            return

        content = message.content or ""
        if "antipromo" in active and self._message_image_attachments(message):
            self._spawn(
                self._check_promotional_images(message, member),
                "scan:promotional-image",
            )
        if member.guild_permissions.administrator or self.is_protected(guild, member.id):
            return
        now = time.monotonic()
        key = (guild.id, member.id)

        if "antimention" in active and len(message.mentions) + len(message.role_mentions) >= 6:
            await self.safe_delete(message)
            self._spawn(self._punish_async(guild, member, "antimention", "Mentioned too many members in one message.",
                                   "Mass mention blocked", "antimention", 0),
                        "punish:antimention-flood")
            return

        if "antipromo" in active and INVITE_RE.search(content):
            await self.safe_delete(message)
            self._spawn(self._punish_async(guild, member, "antipromo", "Posted an invite to another server",
                                   "Server promotion blocked", "antipromo", 3600),
                        "punish:antipromo")
            return

        if "anticaps" in active:
            letters = [char for char in content if char.isalpha()]
            if len(letters) >= 12 and sum(char.isupper() for char in letters) / len(letters) >= 0.7:
                await self.safe_delete(message)
                self._spawn(self.notify(
                    message.channel,
                    build_caps_embed(
                        user_id=member.id,
                        emojis=self._moderation_emojis(guild.id),
                        bot_avatar_url=self._bot_avatar_url(),
                    ),
                ), "notify:anticaps")
                return

        if "antispam" in active:
            times = self.message_times[key]
            times.append(now)
            while times and now - times[0] > 8:
                times.popleft()
            recent = self.last_messages[key]
            recent.append(content.strip().lower())
            duplicate = len(recent) == 3 and len(set(recent)) == 1 and bool(recent[0])
            if len(times) >= 6 or duplicate:
                times.clear()
                recent.clear()
                self._spawn(self.safe_delete(message), "delete:antispam")
                self._spawn(self._purge_and_punish(guild, member, message.channel, "antispam", "Flooded the chat with spam messages"),
                            "punish:antispam")
                return

    async def _check_bot_raid_message(self, message: discord.Message) -> None:
        """Remove and ban non-whitelisted bots that trigger a mass mention."""
        guild = message.guild
        if guild is None:
            return
        active_modules = [
            module
            for module in ("antimention", "antipromo")
            if self.enabled(guild.id, module)
        ]
        if not active_modules:
            return
        if self.is_protected(guild, message.author.id):
            return
        if all(self.whitelisted(guild, message.author.id, module) for module in active_modules):
            return

        content = message.content or ""
        mass_mention = bool(
            getattr(message, "mention_everyone", False) or EVERYONE_RE.search(content)
        )
        if not mass_mention:
            return

        has_invite = bool(INVITE_RE.search(content))
        description = (
            f"Bot/app **{message.author}** (`{message.author.id}`) posted a message "
            "containing a mass mention"
            + (" and a Discord invite." if has_invite else ".")
        )
        try:
            await message.delete()
        except discord.NotFound:
            description += "\nThe message was already deleted."
        except (discord.Forbidden, discord.HTTPException) as error:
            description += (
                f"\nCould not delete the message: {type(error).__name__}. "
                "Check the bot's Manage Messages permission."
            )

        try:
            await guild.ban(
                message.author,
                reason="Raid prevention: bot posted a mass mention",
                delete_message_seconds=0,
            )
            description += "\nThe bot/app account was banned from this server."
        except discord.Forbidden:
            description += (
                "\nCould not ban the bot/app account. Check Ban Members permission "
                "and role hierarchy."
            )
        except discord.HTTPException as error:
            description += (
                f"\nCould not ban the bot/app account: {type(error).__name__}."
            )

        self._spawn(
            self.write_log(
                guild,
                ui.error("Raid Bot Blocked", description),
                dm_owner=True,
            ),
            "log:raid-bot-blocked",
        )

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        if content_type.startswith("image/"):
            return True
        return attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))

    @staticmethod
    def _message_image_attachments(message: discord.Message) -> list[discord.Attachment]:
        attachments: list[discord.Attachment] = list(getattr(message, "attachments", ()))
        for snapshot in getattr(message, "message_snapshots", ()):
            attachments.extend(getattr(snapshot, "attachments", ()))
        return [
            attachment
            for attachment in attachments
            if SecurityCog._is_image_attachment(attachment)
        ][:4]

    async def _check_promotional_images(
        self, message: discord.Message, member: discord.Member
    ) -> None:
        """OCR a small number of image attachments off the message event loop."""
        guild = message.guild
        if guild is None:
            return
        attachments = self._message_image_attachments(message)
        if self._promo_image_scans.locked():
            print(
                f"[security] skipped promotional image scan: scanner is busy "
                f"(guild={guild.id}, message={message.id})"
            )
            return
        for attachment in attachments:
            if attachment.size > 8 * 1024 * 1024:
                print(
                    f"[security] skipped promotional image scan: attachment too large "
                    f"(guild={guild.id}, message={message.id}, size={attachment.size})"
                )
                continue
            try:
                async with self._promo_image_scans:
                    image_bytes = await asyncio.wait_for(attachment.read(), timeout=8)
                    text = await asyncio.wait_for(
                        asyncio.to_thread(image_promo_mod.extract_image_text, image_bytes),
                        timeout=12,
                    )
            except asyncio.TimeoutError:
                print(
                    f"[security] promotional image scan timed out "
                    f"(guild={guild.id}, message={message.id})"
                )
                continue
            except (discord.HTTPException, OSError, ValueError) as error:
                print(
                    f"[security] promotional image scan failed "
                    f"(guild={guild.id}, message={message.id}): "
                    f"{type(error).__name__}: {error}"
                )
                continue

            if not image_promo_mod.looks_like_gambling_promotion(text):
                continue
            try:
                await message.delete()
            except discord.NotFound:
                return
            except (discord.Forbidden, discord.HTTPException) as error:
                print(
                    f"[security] could not delete promotional image "
                    f"(guild={guild.id}, message={message.id}): "
                    f"{type(error).__name__}: {error}"
                )
                return
            warning = await self.notify(
                message.channel,
                build_promo_removed_embed(
                    user_id=member.id,
                    emojis=self._moderation_emojis(guild.id),
                    bot_avatar_url=self._bot_avatar_url(),
                ),
            )
            delete_delay = getattr(self, "promo_warning_delete_delay", 0)
            if warning is not None and delete_delay:
                await asyncio.sleep(delete_delay)
                try:
                    await warning.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                    print(
                        f"[security] promotional warning cleanup skipped: "
                        f"{type(error).__name__}: {error}"
                    )
            return

    # ---------------------------------------------------------------- anti raid
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self._handle_member_join(member)
        except Exception as error:
            print(f"[security] join check failed for {member.id}: {type(error).__name__}: {error}")

    async def _handle_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if member.bot:
            await self._handle_bot_join(member)
            return
        if not self.has_tier(guild.id, "prime"):
            return
        if (
            self.enabled(guild.id, "antialt")
            and not self.whitelisted(guild, member.id, "antialt", member)
            and self._is_alt_account(member)
        ):
            ok, action, problem = await self.punish(
                guild, member.id, "antialt", "Recently-created alt account"
            )
            desc = (
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**Joined as alt:** Account created <t:{int(member.created_at.timestamp())}:R>\n"
                f"**Punishment:** {PUNISHMENTS[action]} — {'applied' if ok else 'FAILED'}"
            )
            if problem:
                desc += f"\n**Reason:** {problem}"
            await self.write_log(guild, ui.error("Alt Account Detected", desc), dm_owner=True)
            return
        if not self.enabled(guild.id, "antiraid"):
            return
        if self.whitelisted(guild, member.id, "antiraid", member):
            return
        now = time.monotonic()
        joins = self.joins[guild.id]
        joins.append((now, member.id))
        while joins and now - joins[0][0] > RAID_WINDOW:
            joins.popleft()

        if now < self.lockdown_until.get(guild.id, 0):
            await self.punish(guild, member.id, "antiraid", "Joined while the server was in raid lockdown")
            return
        if len(joins) >= RAID_JOIN_LIMIT:
            self.lockdown_until[guild.id] = now + RAID_LOCKDOWN_SECONDS
            victims = [user_id for _, user_id in joins]
            joins.clear()
            results = await asyncio.gather(
                *(self.punish(guild, user_id, "antiraid", "Joined during a detected raid") for user_id in victims)
            )
            done = sum(1 for ok, _action, _problem in results if ok)
            action = self.punishment(guild.id, "antiraid")
            desc = (
                f"**{len(victims)}** accounts joined within {RAID_WINDOW} seconds.\n"
                f"**Punishment:** {PUNISHMENTS[action]} — {done}/{len(victims)} applied\n"
                f"Lockdown is active for {RAID_LOCKDOWN_SECONDS // 60} minutes: every new member will be removed automatically."
            )
            await self.write_log(guild, ui.error("Raid Detected", desc), dm_owner=True)

    async def _handle_bot_join(self, member: discord.Member) -> None:
        guild = member.guild
        if not self.enabled(guild.id, "antibot"):
            return
        # Bounded retries: audit-log entries can be delayed after the gateway event.
        actor_id = None
        for attempt in range(5):
            try:
                async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                    if entry.target is not None and entry.target.id == member.id:
                        if entry.created_at is not None:
                            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                            if age > 60:
                                continue
                        actor_id = entry.user_id
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass
            if actor_id is not None:
                break
            await asyncio.sleep(0.5)

        if actor_id is not None:
            if actor_id == guild.owner_id:
                return  # Owner is allowed
            actor_member = guild.get_member(actor_id)
            if actor_member is not None and actor_member.guild_permissions.administrator and self.is_security_admin(guild, actor_member):
                return  # Security admin is allowed

        # Unauthorized or unidentified: kick the bot (safest fallback).
        # Never punish an unrelated human member.
        try:
            await guild.kick(member, reason="Unauthorized bot addition (K.R.Y.O antibot)")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ----------------------------------------------------------- shared actions
    @staticmethod
    def _is_alt_account(member: discord.Member) -> bool:
        age_days = (discord.utils.utcnow() - member.created_at).days
        if age_days < ALT_ACCOUNT_AGE_DAYS:
            return True
        # discord.py 2.x removed Member.avatar_url; use display_avatar.url instead.
        if member.avatar is None:
            url = str(member.display_avatar.url) if member.display_avatar else ""
            if "default" in url.lower():
                return True
        return False

    @staticmethod
    def _protection_status_embed(
        guild: discord.Guild,
        *,
        title: str,
        enabled: bool,
        response: Optional[str] = None,
        warnings: Optional[list[str]] = None,
    ) -> discord.Embed:
        status = "🟢  `ACTIVE`" if enabled else "⚪  `INACTIVE`"
        state_copy = (
            "Monitoring is active for new activity."
            if enabled
            else "Monitoring is paused; this protection will not act."
        )
        embed = ui.security(title, state_copy)
        embed.set_author(name=f"{ui.BRAND}  /  SECURITY CONTROL")
        embed.add_field(name="STATUS", value=status, inline=True)
        embed.add_field(name="SERVER", value=guild.name[:100], inline=True)
        if response:
            embed.add_field(name="AUTOMATED RESPONSE", value=response[:1024], inline=False)
        if warnings:
            warning_text = "\n".join(f"• {warning}" for warning in warnings)
            embed.add_field(name="SETUP CHECK", value=warning_text[:1024], inline=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"KRYO  •  SECURITY CONTROL  •  {guild.id}")
        return embed

    def toggle_module(self, guild: discord.Guild, module: str, on: bool) -> discord.Embed:
        label, _description, _punishable, _default = MODULES[module]
        if on and not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", f"**{label}** needs an active **PRIME** plan.\nRun `.prime tiers` for details.")
        self.cfg(guild.id)["modules"][module] = on
        self.sync_master_flag(guild.id)
        self.save()
        if not on:
            return self._protection_status_embed(
                guild,
                title="Protection Disabled",
                enabled=False,
            )
        response = (
            PUNISHMENTS[self.punishment(guild.id, module)]
            if module in PUNISHABLE
            else "Automatic protection"
        )
        return self._protection_status_embed(
            guild,
            title="Protection Enabled",
            enabled=True,
            response=response,
            warnings=self.permission_warnings(guild),
        )

    def set_all(self, guild: discord.Guild, on: bool) -> discord.Embed:
        if on and not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", "Antinuke needs an active **PRIME** plan.\nRun `.prime tiers` for details.")
        modules = self.cfg(guild.id)["modules"]
        for key in MODULES:
            modules[key] = on
        self.sync_master_flag(guild.id)
        self.save()
        if not on:
            return self._protection_status_embed(
                guild,
                title="All Protections Disabled",
                enabled=False,
            )
        return self.all_protections_enabled_embed(guild)

    def all_protections_enabled_embed(self, guild: discord.Guild) -> discord.Embed:
        total = len(MODULES)
        enabled = sum(self.enabled(guild.id, key) for key in MODULES)
        embed = ui.security(
            "All Protections Enabled",
            f"Spider-Sense is online. {total} protections are watching {guild.name}.\n\nPremium security layer activated.",
        )
        embed.set_author(name=f"{ui.BRAND}  \u2022  SECURITY CONTROL")
        embed.add_field(name="STATUS", value="\U0001f7e2  `ACTIVE`", inline=True)
        embed.add_field(name="SERVER", value=guild.name[:100], inline=True)
        embed.add_field(name="PROTECTIONS", value=f"`{enabled} / {total}`", inline=True)
        embed.add_field(name="RESPONSE", value="Instant", inline=True)

        coverage = []
        for category in CATEGORY_ORDER:
            if category == "all":
                continue
            keys = modules_in_category(category)
            punishment_counts = {key: 0 for key in PUNISHMENTS}
            for key in keys:
                if key in PUNISHABLE:
                    punishment_counts[self.punishment(guild.id, key)] += 1
            actions = "  ".join(
                f"`{PUNISHMENTS[action]} \u00d7 {count}`"
                for action, count in punishment_counts.items()
                if count
            )
            coverage.append(
                f"\U0001f7e3 **{CATEGORY_LABELS[category]}** \u2014 {len(keys)} protections"
                + (f"  {actions}" if actions else "")
            )
        embed.add_field(name="COVERAGE", value="\n".join(coverage), inline=False)
        embed.add_field(
            name="\u200b",
            value="Change punishments any time with `/antinuke panel`.",
            inline=False,
        )
        warnings = self.permission_warnings(guild)
        if warnings:
            embed.add_field(
                name="SETUP CHECK",
                value="\n".join(f"\u2022 {warning}" for warning in warnings)[:1024],
                inline=False,
            )
        embed.set_image(url=f"attachment://{PROTECTIONS_BANNER_FILENAME}")
        embed.set_footer(text=f"KRYO  \u2022  SECURITY CONTROL  \u2022  {guild.id}")
        return embed

    def all_protections_overview_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = ui.security(
            f"All {len(MODULES)} Protections",
            f"Live protection status for **{guild.name}**.",
        )
        for category in CATEGORY_ORDER:
            if category == "all":
                continue
            lines = []
            for key in modules_in_category(category):
                state = "ON" if self.enabled(guild.id, key) else "OFF"
                punishment = (
                    f" \u00b7 `{PUNISHMENTS[self.punishment(guild.id, key)]}`"
                    if key in PUNISHABLE
                    else ""
                )
                lines.append(f"{ui.SPIDER} **{MODULES[key][0]}** \u00b7 `{state}`{punishment}")
            embed.add_field(
                name=f"{CATEGORY_LABELS[category]} \u00b7 {len(lines)}",
                value="\n".join(lines)[:1024] or "No protections",
                inline=False,
            )
        embed.set_footer(text=f"KRYO  \u2022  SECURITY CONTROL  \u2022  {guild.id}")
        return embed

    @staticmethod
    def _is_all_protections_enabled(embed: discord.Embed) -> bool:
        return bool(embed.title and embed.title.endswith("All Protections Enabled"))

    @staticmethod
    def _all_protections_enabled(embed: discord.Embed) -> bool:
        return bool(embed.title and embed.title.endswith("All Protections Enabled"))

    @staticmethod
    def _set_antinuke_activation_banner(embed: discord.Embed) -> None:
        embed.set_image(url=f"attachment://{ANTINUKE_BANNER_FILENAME}")
        embed.set_thumbnail(url=None)

    @staticmethod
    def _set_protections_banner(embed: discord.Embed) -> list[discord.File]:
        embed.set_image(url=f"attachment://{PROTECTIONS_BANNER_FILENAME}")
        embed.set_thumbnail(url=f"attachment://{PROTECTIONS_PREVIEW_FILENAME}")
        return [
            discord.File(PROTECTIONS_BANNER_PATH, filename=PROTECTIONS_BANNER_FILENAME),
            discord.File(PROTECTIONS_PREVIEW_PATH, filename=PROTECTIONS_PREVIEW_FILENAME),
        ]
    def enable_summary_text(self, guild: discord.Guild) -> str:
        total = len(MODULES)
        enabled = sum(self.enabled(guild.id, key) for key in MODULES)
        lines = [
            f"## Spider-Sense is online",
            f"{total} protections are watching **{guild.name}**.",
            "",
            f"\U0001f7e2 **Active**  \u00b7  Response **Instant**  \u00b7  `{enabled} / {total}`",
            "",
            "\u2500" * 28,
            "",
        ]
        for category in CATEGORY_ORDER:
            if category == "all":
                continue
            keys = modules_in_category(category)
            counts = {key: 0 for key in PUNISHMENTS}
            for key in keys:
                if key in PUNISHABLE:
                    counts[self.punishment(guild.id, key)] += 1
            actions = "  ".join(
                f"`{PUNISHMENTS[action]} \u00d7 {count}`" for action, count in counts.items() if count
            )
            lines.append(f"{ui.SPIDER} **{CATEGORY_LABELS[category]}** \u00b7 {len(keys)}" + (f"   {actions}" if actions else ""))
        lines.append("")
        lines.append("Change punishments any time with `/antinuke panel`.")
        warnings = self.permission_warnings(guild)
        if warnings:
            lines.append("")
            lines.append("**Setup check**")
            lines.append("\n".join(f"\u2022 {warning}" for warning in warnings)[:900])
        return "\n".join(lines)[:3800]

    @staticmethod
    def _enable_assets_ready() -> bool:
        return ENABLE_GIF_PATH.exists() and ENABLE_PNG_PATH.exists()

    async def _send_all_enabled_prefix(
        self,
        ctx: commands.Context,
        embed: discord.Embed,
    ) -> None:
        if not self._is_all_protections_enabled(embed):
            await ctx.send(embed=embed)
            return
        if ctx.guild is None:
            await ctx.send(embed=embed)
            return

        if self._enable_assets_ready():
            message = await ctx.send(
                view=AllProtectionsLayout(self, ctx.guild, armed=False),
                file=discord.File(ENABLE_GIF_PATH, filename=ENABLE_GIF_NAME),
            )
            await asyncio.sleep(ANTINUKE_BANNER_DURATION)
            try:
                await message.edit(
                    view=AllProtectionsLayout(self, ctx.guild, armed=True),
                    attachments=[discord.File(ENABLE_PNG_PATH, filename=ENABLE_PNG_NAME)],
                )
            except discord.HTTPException:
                pass
            return

        activation_embed = discord.Embed.from_dict(embed.to_dict())
        activation_embed.set_image(url=f"attachment://{ANTINUKE_BANNER_FILENAME}")
        message = await ctx.send(
            embed=activation_embed,
            file=discord.File(ANTINUKE_BANNER_PATH, filename=ANTINUKE_BANNER_FILENAME),
        )
        await asyncio.sleep(ANTINUKE_BANNER_DURATION)
        await message.edit(
            embed=embed,
            attachments=self._set_protections_banner(embed),
            view=AllProtectionsView(self, ctx.guild),
        )

    def thumb(self) -> Optional[str]:
        return self.bot.user.display_avatar.url if self.bot.user is not None else None

    def status_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = []
        for key, (label, _desc, punishable, _default) in MODULES.items():
            state = "ON" if self.enabled(guild.id, key) else "OFF"
            extra = f" · {PUNISHMENTS[self.punishment(guild.id, key)]}" if punishable else ""
            rows.append((label, f"`{state}{extra}`"))
        settings = self.guild_settings(guild.id)
        log_id = settings.get("log_channel_id")
        emb = ui.panel(
            "Antinuke Status",
            "Live protection status of this server.",
            rows,
            notes=["Use /antinuke panel to choose Ban, Kick or Timeout", "Use /whitelist show to see trusted members"],
            thumbnail=self.thumb(),
        )
        emb.add_field(
            name="Whitelist (all protections)",
            value=f"{len(self.wl_get(guild.id, 'all', 'users'))} users · {len(self.wl_get(guild.id, 'all', 'roles'))} roles",
            inline=True,
        )
        emb.add_field(name="Log channel", value=f"<#{log_id}>" if log_id else "Not set", inline=True)
        emb.add_field(name="Plan", value="Active" if self.has_tier(guild.id, "prime") else "Free (protections inactive)", inline=True)
        warnings = self.permission_warnings(guild)
        if warnings:
            emb.add_field(name="Attention", value="\n".join(f"{ui.SPIDER} {w}" for w in warnings), inline=False)
        return emb

    def panel_embed(self, guild: discord.Guild, category: str = "all", selected: str = "all") -> discord.Embed:
        rows = []
        for key in PUNISHABLE:
            if category != "all" and rules_mod.PROTECTIONS[key].category != category:
                continue
            mark = "  (selected)" if selected == key else ""
            rows.append((MODULES[key][0] + mark, f"`{PUNISHMENTS[self.punishment(guild.id, key)]}`"))
        target = "Every protection" if selected == "all" else MODULES[selected][0]
        scope = CATEGORY_LABELS.get(category, category)
        return ui.panel(
            "Punishment Panel",
            f"Category: **{scope}**\nApplying to: **{target}**\nChoose what happens to an offender.",
            rows,
            notes=[
                "First Menu · choose a category",
                "Second Menu · choose a protection, or everything in the category",
                "Third Menu · choose Ban, Kick or Timeout",
            ],
            thumbnail=self.thumb(),
        )

    # ------------------------------------------------- extended status embeds
    def extended_status_embed(self, guild: discord.Guild) -> discord.Embed:
        """Everything at a glance: category totals, lockdown and snapshots."""
        rows: list[tuple[str, Optional[str]]] = []
        for category in CATEGORY_ORDER:
            if category == "all":
                continue
            keys = [key for key in modules_in_category(category) if key in MODULES]
            if not keys:
                continue
            on = sum(1 for key in keys if self.enabled(guild.id, key))
            rows.append((CATEGORY_LABELS[category], f"`{on}/{len(keys)} ON`"))

        new_on = sum(1 for key in rules_mod.NEW_MODULES if self.enabled(guild.id, key))
        snapshots = len(self.snapshots.history(guild.id))
        state = self.lockdowns.state_of(guild.id)
        lockdown = "**ON**" if state and self.lockdowns.is_active(guild.id) else "OFF"
        if state and self.lockdowns.is_active(guild.id):
            lockdown += f" · {state.remaining() // 60}m left"

        emb = ui.panel(
            "Protection Status",
            "Live protection status of this server.",
            rows,
            notes=[
                "Use /protection toggle to switch the newer protections",
                "Use /backup and /restore for configuration snapshots",
                "Use /lockdown and /panic during an attack",
            ],
            thumbnail=self.thumb(),
        )
        emb.add_field(name="New protections", value=f"`{new_on}/{len(rules_mod.NEW_MODULES)} ON`", inline=True)
        emb.add_field(name="Lockdown", value=lockdown, inline=True)
        emb.add_field(name="Snapshots", value=str(snapshots), inline=True)
        emb.add_field(
            name="Whitelist",
            value=f"{len(self.wl_get(guild.id, 'all', 'users'))} users · {len(self.wl_get(guild.id, 'all', 'roles'))} roles",
            inline=True,
        )
        warnings = self.permission_warnings(guild)
        if warnings:
            emb.add_field(name="Attention", value="\n".join(f"{ui.SPIDER} {w}" for w in warnings), inline=False)
        return emb

    def snapshots_embed(self, guild: discord.Guild) -> discord.Embed:
        history = self.snapshots.history(guild.id)
        if not history:
            return ui.warn(
                "No Snapshots",
                "Nothing is stored yet. Run `/backup` before a risky change so you can undo it.",
            )
        lines = [
            f"{ui.SPIDER} {snap_mod.describe(item)}{' · **pinned**' if item.get('pinned') else ''}"
            for item in reversed(history[:12])
        ]
        return ui.panel(
            "Stored Snapshots",
            f"`{len(history)}` snapshot(s) kept. Use `/restore` to roll the server back.",
            lines,
            thumbnail=self.thumb(),
        )

    def security_logs_embed(self, guild: discord.Guild) -> discord.Embed:
        events = list(self.security_log.get(guild.id, []))
        if not events:
            return ui.warn(
                "No Security Events",
                "Nothing has been detected since this bot started. Events appear here and in your log channel.",
            )
        lines = []
        for item in reversed(events[-15:]):
            flag = "applied" if item.get("applied") else "FAILED"
            protection_key = item.get("protection")
            protection_name = MODULES.get(str(protection_key), ("?",))[0] if isinstance(protection_key, str) else "?"
            lines.append(
                f"{ui.SPIDER} **{item.get('severity')}** · {protection_name} · "
                f"executor `{item.get('executor')}` · {flag}\n"
                f"> {str(item.get('reason'))[:160]}"
            )
        return ui.panel(
            "Security Events",
            f"Last **{len(events)}** detection(s) since this bot started.",
            lines,
            notes=["Events are also written to your security log channel"],
            thumbnail=self.thumb(),
        )

    def whitelist_embed(self, guild: discord.Guild) -> discord.Embed:
        rows = [(label, None) for _key, label in WL_OPTIONS]
        return ui.panel(
            "Antinuke Whitelist",
            "Select the category to see whitelisted users/roles from each protection.",
            rows,
            notes=["Use First Menu to View Whitelisted Users", "Use Second Menu to View Whitelisted Roles"],
            thumbnail=self.thumb(),
        )

    def _wl_label(self, key: str) -> str:
        if key == "all":
            return "All protections"
        if key in CATEGORY_LABELS and key not in MODULES:
            return CATEGORY_LABELS[key]
        return MODULES[key][0]

    def whitelist_list_embed(self, guild: discord.Guild, key: str, kind: str) -> discord.Embed:
        label = self._wl_label(key)
        fmt = "<@{}>" if kind == "users" else "<@&{}>"

        def lines(module: str) -> str:
            return "\n".join(f"{ui.SPIDER} {fmt.format(i)} (`{i}`)" for i in self.wl_get(guild.id, module, kind))

        title = f"Whitelisted {'Users' if kind == 'users' else 'Roles'} : {label}"
        emb = ui.security(title, lines(key)[:4000] or f"Nobody is whitelisted in **{label}**.")
        if key != "all" and self.wl_get(guild.id, "all", kind):
            emb.add_field(name="Also whitelisted for all protections", value=lines("all")[:1000], inline=False)
        return emb

    def whitelist_change(self, guild: discord.Guild, target, add: bool, module: str = "all") -> discord.Embed:
        kind = "roles" if isinstance(target, discord.Role) else "users"
        label = self._wl_label(module)
        current = self.wl_get(guild.id, module, kind)
        if add:
            if target.id in current:
                return build_already_whitelisted_embed(
                    user=target,
                    category=label,
                    emojis=self._whitelist_emojis(guild),
                    bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                )
            current.append(target.id)
            action = "added"
        else:
            if target.id not in current:
                return build_not_whitelisted_embed(
                    user=target,
                    category=label,
                    emojis=self._whitelist_emojis(guild),
                    bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
                )
            current.remove(target.id)
            action = "removed"
        self.wl_set(guild.id, module, kind, current)
        self.save()
        return build_whitelist_updated_embed(
            user=target,
            category=label,
            action=action,
            emojis=self._whitelist_emojis(guild),
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )

    def _whitelist_emojis(self, guild: discord.Guild) -> dict[str, str]:
        mass_cog = self.bot.get_cog("MassMentionCog")
        return dict(getattr(mass_cog, "emojis", {}).get(guild.id, {}))

    @app_commands.command(name="testwhitelist", description="Preview whitelist status embeds.")
    @app_commands.guild_only()
    @admin_only_slash
    async def testwhitelist(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        emojis = self._whitelist_emojis(guild)
        avatar_url = self.bot.user.display_avatar.url if self.bot.user else None
        await interaction.response.send_message(
            embeds=[
                build_not_whitelisted_embed(
                    user=interaction.user,
                    category="All protections",
                    emojis=emojis,
                    bot_avatar_url=avatar_url,
                ),
                build_whitelist_updated_embed(
                    user=interaction.user,
                    category="All protections",
                    action="added",
                    emojis=emojis,
                    bot_avatar_url=avatar_url,
                ),
            ],
            ephemeral=True,
        )

    @app_commands.command(name="testembeds", description="Preview moderation result embeds.")
    @app_commands.guild_only()
    @admin_only_slash
    async def testembeds(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        emojis = self._moderation_emojis(guild.id)
        avatar_url = self._bot_avatar_url()
        embeds = [
            build_ban_embed(
                username="Example User",
                reason="Example moderation reason",
                emojis=emojis,
                bot_avatar_url=avatar_url,
            ),
            build_unban_embed(
                username="Example User",
                emojis=emojis,
                bot_avatar_url=avatar_url,
            ),
            build_promo_removed_embed(
                user_id=interaction.user.id,
                emojis=emojis,
                bot_avatar_url=avatar_url,
            ),
            build_caps_embed(
                user_id=interaction.user.id,
                emojis=emojis,
                bot_avatar_url=avatar_url,
            ),
            build_link_blocked_embed(
                user_id=interaction.user.id,
                emojis=emojis,
                bot_avatar_url=avatar_url,
            ),
        ]
        try:
            await interaction.response.send_message(embeds=embeds, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"[security] moderation embed preview could not be sent: {type(error).__name__}: {error}")

    def set_log_channel(self, guild: discord.Guild, channel: discord.TextChannel) -> discord.Embed:
        self.guild_settings(guild.id)["log_channel_id"] = channel.id
        self.save()
        return ui.success("Log Channel Set", f"Security logs will now be sent to {channel.mention}.")

    def set_limits(self, guild: discord.Guild, actions: int, seconds: int) -> discord.Embed:
        settings = self.guild_settings(guild.id)
        settings["antinuke_threshold"] = actions
        settings["antinuke_window_seconds"] = seconds
        self.save()
        return ui.success(
            "Limits Updated",
            f"Mass ban, kick, integration and update actions are punished after **{actions}** actions within **{seconds}** seconds.\n"
            "Channel delete, role delete, bot add, webhook create, integration create and server setting changes are punished on the first attempt.",
        )

    # -------------------------------------------------------------- slash commands
    async def slash_toggle(self, interaction: discord.Interaction, module: str, on: bool) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.toggle_module(guild, module, on), ephemeral=False)

    async def slash_toggle_all(self, interaction: discord.Interaction, on: bool) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        embed = self.set_all(guild, on)
        if not self._is_all_protections_enabled(embed):
            return await ui.respond(interaction, embed, ephemeral=False)

        if self._enable_assets_ready():
            await interaction.response.send_message(
                view=AllProtectionsLayout(self, guild, armed=False),
                file=discord.File(ENABLE_GIF_PATH, filename=ENABLE_GIF_NAME),
                ephemeral=False,
            )
            await asyncio.sleep(ANTINUKE_BANNER_DURATION)
            try:
                await interaction.edit_original_response(
                    view=AllProtectionsLayout(self, guild, armed=True),
                    attachments=[discord.File(ENABLE_PNG_PATH, filename=ENABLE_PNG_NAME)],
                )
            except discord.HTTPException:
                pass
            return

        activation_embed = discord.Embed.from_dict(embed.to_dict())
        self._set_antinuke_activation_banner(activation_embed)
        await interaction.response.send_message(
            embed=activation_embed,
            file=discord.File(
                ANTINUKE_BANNER_PATH,
                filename=ANTINUKE_BANNER_FILENAME,
            ),
            ephemeral=False,
        )
        await asyncio.sleep(ANTINUKE_BANNER_DURATION)
        await interaction.edit_original_response(
            embed=embed,
            attachments=self._set_protections_banner(embed),
            view=AllProtectionsView(self, guild),
        )

    whitelist = app_commands.Group(
        name="whitelist",
        description="Manage trusted users and roles",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @whitelist.command(name="add", description="Whitelist a user or a role")
    @app_commands.describe(category="Which protection to whitelist for (default: all)")
    @app_commands.choices(category=WL_CHOICES)
    @admin_only_slash
    async def whitelist_add(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        if member is None and role is None:
            await ui.respond(interaction, ui.warn("Nothing Selected", "Pick a member, a role, or both."), ephemeral=True)
            return
        key = category.value if category else "all"
        for target in (member, role):
            if target is not None:
                await ui.respond(interaction, self.whitelist_change(guild, target, True, key))

    @whitelist.command(name="remove", description="Remove a user or a role from the whitelist")
    @app_commands.describe(category="Which protection to remove from (default: all)")
    @app_commands.choices(category=WL_CHOICES)
    @admin_only_slash
    async def whitelist_remove(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        if member is None and role is None:
            await ui.respond(interaction, ui.warn("Nothing Selected", "Pick a member, a role, or both."), ephemeral=True)
            return
        key = category.value if category else "all"
        for target in (member, role):
            if target is not None:
                await ui.respond(interaction, self.whitelist_change(guild, target, False, key))

    @whitelist.command(name="show", description="Show whitelisted users and roles")
    @admin_only_slash
    async def whitelist_show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.whitelist_embed(guild), view=WhitelistView(self, guild))

    @app_commands.command(name="setlog", description="Choose the channel for security logs")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def setlog_slash(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.set_log_channel(guild, channel))

    protection = app_commands.Group(
        name="protection",
        description="Manage the wider protection layer",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @protection.command(name="toggle", description="Turn one of the newer protections on or off")
    @app_commands.describe(module="Which protection", state="On or off")
    @app_commands.choices(
        module=[app_commands.Choice(name=MODULES[key][0], value=key) for key in NEW_MODULE_KEYS[:25]],
        state=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off", value="off")],
    )
    @admin_only_slash
    async def protection_toggle(
        self,
        interaction: discord.Interaction,
        module: app_commands.Choice[str],
        state: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.toggle_module(guild, module.value, state.value == "on"))

    @protection.command(name="list", description="Show every protection and its punishment")
    @admin_only_slash
    async def protection_list(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.extended_status_embed(guild))

    @protection.command(name="threshold", description="Change how many actions trigger one protection")
    @app_commands.describe(module="Which protection", actions="Actions before it acts (1-20)")
    @app_commands.choices(module=[app_commands.Choice(name=MODULES[key][0], value=key) for key in NEW_MODULE_KEYS[:25]])
    @admin_only_slash
    async def protection_threshold(
        self,
        interaction: discord.Interaction,
        module: app_commands.Choice[str],
        actions: app_commands.Range[int, 1, 20],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        thresholds = dict(self.security_thresholds(guild.id) or {})
        thresholds[module.value] = actions
        self.set_thresholds(guild.id, thresholds)
        await ui.respond(
            interaction,
            ui.success(
                "Threshold Updated",
                f"**{MODULES[module.value][0]}** now acts after **{actions}** actions inside the window.",
            ),
        )

    @protection.command(name="snapshots", description="List the stored server snapshots")
    @admin_only_slash
    async def protection_snapshots(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.snapshots_embed(guild))

    @app_commands.command(name="status", description="Full protection status for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def status_slash(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.extended_status_embed(guild))

    @app_commands.command(name="backup", description="Save a snapshot you can restore later")
    @app_commands.describe(label="What this snapshot is for")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def backup_slash(self, interaction: discord.Interaction, label: Optional[str] = None) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await interaction.response.defer(thinking=True)
        embed = await self.do_backup(guild, label)
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="restore", description="Restore the server from a snapshot")
    @app_commands.describe(snapshot="Snapshot id, or leave empty for the newest")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def restore_slash(self, interaction: discord.Interaction, snapshot: Optional[str] = None) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await interaction.response.defer(thinking=True)
        embed = await self.do_restore(guild, snapshot)
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="lockdown", description="Lock or unlock the server during an attack")
    @app_commands.describe(action="Lock or unlock", minutes="How long to lock for (1-60)")
    @app_commands.choices(
        action=[app_commands.Choice(name="Lock", value="on"), app_commands.Choice(name="Unlock", value="off")]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def lockdown_slash(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        minutes: app_commands.Range[int, 1, 60] = 15,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await interaction.response.defer(thinking=True)
        embed = await self.do_lockdown(guild, action.value == "on", minutes)
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="panic", description="Emergency: lock the server and restore the last snapshot")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def panic_slash(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await interaction.response.defer(thinking=True)
        embed = await self.do_panic(guild)
        await interaction.edit_original_response(embed=embed)

    # `seclogs` rather than `logs`, because the log suite already owns /logs and .logs
    @app_commands.command(name="seclogs", description="Recent security events in this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only_slash
    async def logs_slash(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
        await ui.respond(interaction, self.security_logs_embed(guild))

    # ---------------------------------------------------- extended command bodies
    async def do_backup(self, guild: discord.Guild, label: Optional[str] = None) -> discord.Embed:
        if not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", "Snapshots need an active **PRIME** plan.")
        try:
            state = await snap_mod.capture(guild)
        except Exception as error:
            return ui.error("Backup Failed", f"I could not read this server ({type(error).__name__}: {error}).")
        snapshot = self.snapshots.add(guild.id, state, label=label or "manual backup")
        return ui.success(
            "Snapshot Saved",
            f"**{snap_mod.describe(snapshot)}**\nUse `/restore` to roll the server back to this point.",
        )

    async def do_restore(self, guild: discord.Guild, snapshot_id: Optional[str] = None) -> discord.Embed:
        if not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", "Snapshots need an active **PRIME** plan.")
        snapshot = self._find_snapshot(guild, snapshot_id)
        if snapshot is None:
            return ui.warn("No Snapshot", "There is nothing stored yet. Run `/backup` first.")
        report = await snap_mod.restore_all(guild, snapshot, "Security: restoring a snapshot")
        head = f"**{snap_mod.describe(snapshot)}**\n{report.summary()}\n{report.lines()}"
        return ui.success("Server Restored", head) if report.status == snap_mod.RESTORED else ui.warn("Partly Restored", head)

    def _find_snapshot(self, guild: discord.Guild, snapshot_id: Optional[str]) -> Optional[dict]:
        """Resolve a snapshot reference from a user-supplied string."""
        if snapshot_id is None:
            return self.snapshots.latest(guild.id)
        try:
            stamp = float(str(snapshot_id).strip().lstrip("#"))
        except (TypeError, ValueError):
            return None
        return self.snapshots.by_id(guild.id, stamp)

    async def do_lockdown(self, guild: discord.Guild, on: bool, minutes: int = 15) -> discord.Embed:
        if not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", "Lockdown needs an active **PRIME** plan.")
        if on:
            result = await self.lockdowns.engage(guild, reason="locked by an admin", triggered_by="admin", duration=minutes * 60)
            if not result.engaged:
                current = self.lockdowns.state_of(guild.id)
                return ui.warn("Already Locked", current.summary() if current is not None else "This server is already locked.")
            notes = "\n".join(f"{ui.SPIDER} {note}" for note in result.notes)
            return ui.error(
                "Server Locked",
                f"**{len(result.state.touched)}** channel(s) locked for **{minutes} minutes**.\n{notes}",
            )
        current = self.lockdowns.state_of(guild.id)
        if current is None or not current.active:
            return ui.warn("Not Locked", "This server was not in a lockdown.")
        result = await self.lockdowns.release(guild)
        if result.state.notes and any(note.startswith("could not") for note in result.state.notes):
            notes = "\n".join(f"{ui.SPIDER} {note}" for note in result.state.notes)
            return ui.warn("Partly Unlocked", notes)
        return ui.success("Server Unlocked", result.state.summary())

    async def do_panic(self, guild: discord.Guild) -> discord.Embed:
        if not self.has_tier(guild.id, "prime"):
            return ui.error("Plan Required", "Panic mode needs an active **PRIME** plan.")
        latest = self.snapshots.latest(guild.id)
        desc = ""
        if latest is not None:
            report = await snap_mod.restore_all(guild, latest, "Security: panic restore")
            desc += f"**Restored:** {report.summary()}\n{report.lines()}"
        else:
            desc += "**No snapshot was stored**, so nothing could be restored."
        lock = await self.lockdowns.engage(
            guild,
            reason="panic button",
            triggered_by="admin",
            duration=lockdown_mod.MAX_DURATION,
        )
        desc += f"\n**Channels locked:** {len(lock.state.touched)}"
        if lock.notes:
            desc += "\n" + "\n".join(f"{ui.SPIDER} {note}" for note in lock.notes)
        return ui.error("Panic Handled", desc + "\nCheck the security log channel for the full detail.")

    # ---------------------------------------------------------------- dot commands
    @commands.command(name="secadmin", brief="Create a role with Manage Roles permission (role manager only).",
                      extras={"category": "Anti Nuke", "usage": ".secadmin [role_name]", "public": False})
    @commands.guild_only()
    @admin_only_prefix
    async def secadmin_prefix(self, ctx: commands.Context, *, role_name: str = "Security Admin") -> Any:
        guild = ctx.guild
        if guild is None:
            return None
        author = ctx.author
        if not isinstance(author, discord.Member):
            return None
        bot_member = guild.me

        existing = discord.utils.get(guild.roles, name=role_name)
        if existing is not None:
            return await ctx.send(embed=ui.warn("Role Exists", f"`{role_name}` already exists. Delete it first or pick a different name."))

        me = bot_member
        if not me.guild_permissions.manage_roles:
            return await ctx.send(embed=ui.error("Missing Permission", "I need **Manage Roles** to create this role."))

        my_top = max((r.position for r in me.roles), default=0)
        author_top = max((r.position for r in author.roles), default=0)
        if author_top >= my_top:
            return await ctx.send(embed=ui.error("Role Hierarchy", "My top role is below or equal to yours. Move my role above yours first."))

        position = min(my_top - 1, max(author_top + 1, 1))
        full_perms = discord.Permissions(manage_roles=True)

        try:
            role = await guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                hoist=True,
                mentionable=True,
                reason=f"Role-manager role created by {author}",
            )
            await role.edit(position=position, reason="Position below bot, above invoker.")
            await role.edit(permissions=full_perms, reason="Grant Manage Roles only")
            fresh = await guild.fetch_role(role.id)
            if not fresh.permissions.manage_roles:
                return await ctx.send(embed=ui.error("Permission Error", "Discord did not persist Manage Roles on this role. Check my role position."))
            await author.add_roles(role, reason="Auto-assign secadmin to creator")

        except discord.Forbidden:
            return await ctx.send(embed=ui.error("Missing Permission", "I could not create, position, edit, or assign that role."))
        except discord.HTTPException as exc:
            return await ctx.send(embed=ui.error("Discord Error", f"{type(exc).__name__}: {exc}"))

        self.guild_settings(guild.id)["security_admin_role_id"] = role.id
        self.save()

        lines = [
            f"**Role created:** `{role_name}` (`{role.id}`)",
            f"**Assigned to:** {author.mention}",
            "",
            "**Permission granted:** Manage Roles only",
            "",
            f"**Position:** {position} (below bot, above invoker)",
            "",
            "**This role can only assign/remove roles.** It cannot ban, kick, or timeout anyone.",
            "",
            "Admins with **Administrator** permission can use slash commands and mod panel directly.",
        ]
        await ctx.send(embed=ui.success("Full Security Admin Created", "\n".join(lines)))

    @commands.group(name="antinuke", invoke_without_command=True, brief="Antinuke: on, off, status, panel, limits.",
                    extras={"category": "Anti Nuke", "usage": ".antinuke on|off|status|panel|limits"})
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.status_embed(guild))

    @antinuke_prefix.command(name="on", brief="Turn every protection on.")
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_on(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await self._send_all_enabled_prefix(ctx, self.set_all(guild, True))

    @antinuke_prefix.command(name="off", brief="Turn every protection off.")
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_off(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.set_all(guild, False))

    @antinuke_prefix.command(name="status", brief="Show protection status.")
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_status_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.status_embed(guild))

    @antinuke_prefix.command(name="panel", brief="Choose Ban, Kick or Timeout per protection.")
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_panel_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.panel_embed(guild), view=PunishmentPanel(self, guild))

    @antinuke_prefix.command(name="limits", brief="Set action limit and window.")
    @commands.guild_only()
    @admin_only_prefix
    async def antinuke_limits_prefix(self, ctx: commands.Context, actions: int = 3, window_seconds: int = 10) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        if not 1 <= actions <= 10 or not 5 <= window_seconds <= 60:
            await ctx.send(embed=ui.warn("Invalid Limits", "Actions must be 1-10 and the window 5-60 seconds."))
            return
        await ctx.send(embed=self.set_limits(guild, actions, window_seconds))

    @commands.command(name="status", brief="Show protection status.", extras={"category": "Anti Nuke", "usage": ".status"})
    @commands.guild_only()
    @admin_only_prefix
    async def status_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.status_embed(guild))

    @commands.command(name="panel", brief="Open the punishment panel.", extras={"category": "Anti Nuke", "usage": ".panel"})
    @commands.guild_only()
    @admin_only_prefix
    async def panel_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.panel_embed(guild, "all", "all"), view=PunishmentPanel(self, guild))

    @commands.group(name="wl", invoke_without_command=True, brief="Whitelist a user or role.",
                    extras={"category": "Anti Nuke", "usage": ".wl @user|@role [category]  ·  .wl show"})
    @commands.guild_only()
    @admin_only_prefix
    async def wl_prefix(
        self, ctx: commands.Context, target: Optional[Union[discord.Member, discord.Role]] = None, category: str = "all"
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        if target is None:
            await ctx.send(embed=self.whitelist_embed(guild), view=WhitelistView(self, guild))
            return
        key = category.lower()
        if key != "all" and key not in MODULES and key not in CATEGORY_LABELS:
            await ctx.send(
                embed=ui.warn(
                    "Unknown Category",
                    "Use a category or a protection name:\n"
                    + "\n".join(f"{ui.SPIDER} `{name}`" for name in ["all", *CATEGORY_ORDER[1:], *NEW_MODULE_KEYS]),
                )
            )
            return
        await ctx.send(embed=self.whitelist_change(guild, target, True, key))
    @wl_prefix.command(name="show", brief="Show the whitelist.")
    @commands.guild_only()
    @admin_only_prefix
    async def wl_show(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.whitelist_embed(guild), view=WhitelistView(self, guild))

    @commands.command(name="unwl", brief="Remove a user or role from the whitelist.",
                      extras={"category": "Anti Nuke", "usage": ".unwl @user|@role [category]"})
    @commands.guild_only()
    @admin_only_prefix
    async def unwl_prefix(self, ctx: commands.Context, target: Union[discord.Member, discord.Role], category: str = "all") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        key = category.lower()
        if key != "all" and key not in MODULES and key not in CATEGORY_LABELS:
            await ctx.send(
                embed=ui.warn(
                    "Unknown Category",
                    "Use a category or a protection name:\n"
                    + "\n".join(f"{ui.SPIDER} `{name}`" for name in ["all", *CATEGORY_ORDER[1:], *NEW_MODULE_KEYS]),
                )
            )
            return
        await ctx.send(embed=self.whitelist_change(guild, target, False, key))

    @commands.command(name="setlog", brief="Set the security log channel.",
                      extras={"category": "Anti Nuke", "usage": ".setlog #channel"})
    @commands.guild_only()
    @admin_only_prefix
    async def setlog_prefix(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.set_log_channel(guild, channel))

    @commands.group(name="protection", invoke_without_command=True, brief="Extended protection layer.",
                    extras={"category": "Anti Nuke", "usage": ".protection status|toggle <name> on|off|threshold <name> <n>"})
    @commands.guild_only()
    @admin_only_prefix
    async def protection_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.extended_status_embed(guild))

    @protection_prefix.command(name="status", brief="Full protection status.")
    @commands.guild_only()
    @admin_only_prefix
    async def protection_status_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.extended_status_embed(guild))

    @protection_prefix.command(name="toggle", brief="Turn one of the newer protections on or off.")
    @commands.guild_only()
    @admin_only_prefix
    async def protection_toggle_prefix(self, ctx: commands.Context, module: str, state: Optional[str] = None) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        value = parse_state(state)
        if value is None or module.lower() not in NEW_MODULE_KEYS:
            await ctx.send(
                embed=ui.warn(
                    "Usage",
                    f"`.protection toggle <name> on|off`\nNames: "
                    + ", ".join(f"`{key}`" for key in NEW_MODULE_KEYS[:25]),
                )
            )
            return
        await ctx.send(embed=self.toggle_module(guild, module.lower(), value))

    @protection_prefix.command(name="threshold", brief="Actions before one protection acts.")
    @commands.guild_only()
    @admin_only_prefix
    async def protection_threshold_prefix(self, ctx: commands.Context, module: str, actions: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        key = module.lower()
        if key not in MODULES:
            await ctx.send(embed=ui.warn("Unknown Protection", f"There is no protection called `{module}`."))
            return
        thresholds = dict(self.security_thresholds(guild.id) or {})
        thresholds[key] = max(1, min(20, actions))
        self.set_thresholds(guild.id, thresholds)
        await ctx.send(
            embed=ui.success(
                "Threshold Updated",
                f"**{MODULES[key][0]}** now acts after **{thresholds[key]}** actions inside the window.",
            )
        )

    @commands.command(name="backup", brief="Save a server snapshot you can restore later.",
                      extras={"category": "Anti Nuke", "usage": ".backup [label]"})
    @commands.guild_only()
    @admin_only_prefix
    async def backup_prefix(self, ctx: commands.Context, label: Optional[str] = None) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=await self.do_backup(guild, label))

    @commands.command(name="restore", brief="Restore the server from a snapshot.",
                      extras={"category": "Anti Nuke", "usage": ".restore [snapshot]"})
    @commands.guild_only()
    @admin_only_prefix
    async def restore_prefix(self, ctx: commands.Context, snapshot: Optional[str] = None) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=await self.do_restore(guild, snapshot))

    @protection_prefix.command(name="snapshots", brief="List the stored snapshots.")
    @commands.guild_only()
    @admin_only_prefix
    async def protection_snapshots_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.snapshots_embed(guild))

    @commands.command(name="lockdown", brief="Lock or unlock the server during an attack.",
                      extras={"category": "Anti Nuke", "usage": ".lockdown on|off [minutes]"})
    @commands.guild_only()
    @admin_only_prefix
    async def lockdown_prefix(self, ctx: commands.Context, state: str = "on", minutes: int = 15) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        value = parse_state(state)
        if value is None:
            await ctx.send(embed=ui.warn("Usage", "`.lockdown on [minutes]` or `.lockdown off`"))
            return
        await ctx.send(embed=await self.do_lockdown(guild, value, max(1, min(60, minutes))))

    @commands.command(name="panic", brief="Emergency: lock the server and restore the last snapshot.",
                      extras={"category": "Anti Nuke", "usage": ".panic"})
    @commands.guild_only()
    @admin_only_prefix
    async def panic_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=await self.do_panic(guild))

    # Named `seclogs` rather than `logs` because the log suite already owns `.logs`.
    @commands.command(name="seclogs", brief="Recent security events in this server.",
                      extras={"category": "Anti Nuke", "usage": ".seclogs"})
    @commands.guild_only()
    @admin_only_prefix
    async def logs_prefix(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used in a server."))
            return
        await ctx.send(embed=self.security_logs_embed(guild))
