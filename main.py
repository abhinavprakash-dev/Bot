import re
import random
import asyncio
import gc
import sys
import time
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import urlsplit
import discord
import wavelink
import logging
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands
from botcore.config import Settings
from botcore.dashboard_api import DashboardAPI, DashboardConfig
from botcore.storage import SQLiteStateStore
from botcore.moderation_embeds import build_notice_dm, build_warning_dm, moderation_emojis_for
from cogs.logging import LoggingCog
from cogs.security import SecurityCog
from cogs.ownerhub import OwnerHub
from cogs.help import HelpCog
from cogs.logsuite import LogSuite
from cogs.staff import RoleConfigCog, ModerationCog, SmartModCog, VoiceToolsCog, action_embed, try_dm
from cogs.community import AutoRoleCog, WelcomerCog, SelfRolesCog, JoinToCreateCog
from cogs.general import UtilityCog, OtherCog, NoPrefixCog, announcement_embed
from cogs.invites import InviteTrackerCog
from cogs.tickets import TicketCog
from cogs.jail import JailCog
from cogs.jokes import JokesCog
from cogs.vcmove import VCMoveCog
from cogs.mass_mention import MassMentionCog
from cogs.link_filter import LinkFilterCog
from botcore.link_filter import LinkFilterConfig
from botcore import ui, log_exception

logging.getLogger("wavelink").setLevel(logging.CRITICAL)
load_dotenv()

CONFIG = Settings.from_env()
TOKEN = CONFIG.discord_token
LAVALINK_URI = CONFIG.lavalink_uri
LAVALINK_PASSWORD = CONFIG.lavalink_password
PREMIUM_NICKNAME_BASE = CONFIG.premium_nickname_base
PREMIUM_TICK_EMOJI = ui.SPIDER

STORE = SQLiteStateStore(CONFIG.database_path, Path(__file__).with_name("data.json"))


def load():
    return STORE.load_state()

data = load()
data.setdefault("carmeet_applications", {})
data.setdefault("settings", {})
data.setdefault("subscriptions", {})
data.setdefault("warnings", {})
data.setdefault("mass_mention_warnings", {})
data.setdefault("noprefix", [])
data.setdefault("event", {})
data.setdefault("tournament", {})
data.setdefault("next_id", 1)

REVIEW_CHANNEL_ID = CONFIG.review_channel_id
ACCEPTED_ROLE_ID = CONFIG.accepted_role_id
STAFF_ROLE_ID = CONFIG.staff_role_id
LOG_CHANNEL_ID = CONFIG.log_channel_id
message_bursts = defaultdict(deque)

SAVE_INTERVAL = 2.0
state_dirty = False


def save():
    global state_dirty
    state_dirty = True


async def flush_state():
    global state_dirty
    if not state_dirty:
        return
    state_dirty = False
    try:
        await STORE.save_state_async(data)
    except Exception as error:
        state_dirty = True
        print(f"State flush failed ({type(error).__name__}: {error}); will retry.")


async def state_writer_loop():
    while not bot.is_closed():
        try:
            await asyncio.sleep(SAVE_INTERVAL)
            await flush_state()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"State writer iteration failed ({type(error).__name__}: {error}); retrying.")
            await asyncio.sleep(SAVE_INTERVAL)


async def loop_lag_loop():
    samples = deque(maxlen=20)
    expected = time.perf_counter() + 1.0
    while not bot.is_closed():
        try:
            await asyncio.sleep(1.0)
            now = time.perf_counter()
            samples.append(max(0.0, (now - expected) * 1000))
            expected = now + 1.0
            bot.loop_lag_avg_ms = sum(samples) / len(samples)
            bot.loop_lag_max_ms = max(samples)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Loop lag monitor failed ({type(error).__name__}: {error}); retrying.")
            expected = time.perf_counter() + 1.0


PREMIUM_TIERS = {"prime": 300}
TIER_LEVELS = {"free": 0, "prime": 1}

_TIER_CACHE = {}
_TIER_CACHE_TTL = 30.0


def _invalidate_tier_cache(guild_id=None):
    if guild_id is None:
        _TIER_CACHE.clear()
    else:
        _TIER_CACHE.pop(str(guild_id), None)


def active_tier(guild_id):
    key = str(guild_id)
    cached = _TIER_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    subscription = data["subscriptions"].get(key)
    tier = "free"
    if subscription:
        try:
            expires_at = datetime.fromisoformat(subscription["expires_at"])
        except (KeyError, TypeError, ValueError):
            expires_at = None
        if expires_at is not None and expires_at > datetime.now(timezone.utc):
            stored = subscription.get("tier", "free")
            tier = "prime" if stored in TIER_LEVELS and stored != "free" else "free"
    _TIER_CACHE[key] = (tier, now + _TIER_CACHE_TTL)
    return tier


def has_tier(guild_id, required_tier):
    return TIER_LEVELS[active_tier(guild_id)] >= TIER_LEVELS[required_tier]


def require_tier(required_tier):
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not has_tier(ctx.guild.id, required_tier):
            raise commands.CheckFailure(
                f"This feature requires the `{required_tier}` plan. Run `.prime tiers` for details."
            )
        return True

    return commands.check(predicate)


def new_id():
    i = data["next_id"]
    data["next_id"] += 1
    return str(i)


def find_by_message(kind, message_id):
    for entry in data[kind].values():
        if entry["message_id"] == message_id:
            return entry
    return None


def make_embed(kind, e):
    color = {"open": discord.Color.green(), "live": discord.Color.orange()}.get(
        e["status"], discord.Color.dark_grey()
    )
    icon = ui.SPIDER
    embed = discord.Embed(
        title=f"{icon} {e['name']}  (ID: {e['id']})",
        description=e.get("desc") or "",
        color=color,
    )
    embed.add_field(name="Status", value=e["status"].upper(), inline=True)
    limit = f"/{e['max']}" if kind == "tournament" else ""
    embed.add_field(name="Players", value=f"{len(e['players'])}{limit}", inline=True)
    if e["players"]:
        embed.add_field(
            name="Joined",
            value=", ".join(f"<@{u}>" for u in e["players"])[:1000],
            inline=False,
        )
    embed.set_footer(text=f"Host: {e['host_name']}")
    return embed


def bracket_text(t):
    if not t["rounds"]:
        return "The bracket has not been generated yet."
    lines = []
    for r, matches in enumerate(t["rounds"], start=1):
        lines.append(f"**Round {r}**")
        for i, (p1, p2, w) in enumerate(matches, start=1):
            right = f"<@{p2}>" if p2 else "BYE"
            win = f"  ➜ {ui.SPIDER} <@{w}>" if w else ""
            lines.append(f"`#{i}` <@{p1}> vs {right}{win}")
    return "\n".join(lines)


class JoinView(discord.ui.View):
    def __init__(self, kind):
        super().__init__(timeout=None)
        self.kind = kind
        self.join.custom_id = f"{kind}_join"
        self.leave.custom_id = f"{kind}_leave"

    @discord.ui.button(label="Join", style=discord.ButtonStyle.green)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        message = interaction.message
        if message is None:
            return await ui.respond(interaction, ui.warn("Invalid Message", "This action must be used from a message."), ephemeral=True)
        e = find_by_message(self.kind, message.id)
        if not e or e["status"] != "open":
            return await ui.respond(interaction, ui.warn("Registration Closed", "Registration for this event is closed."), ephemeral=True)
        uid = interaction.user.id
        if uid in e["players"]:
            return await ui.respond(interaction, ui.warn("Already Registered", "You have already joined this event."), ephemeral=True)
        if self.kind == "tournament" and len(e["players"]) >= e["max"]:
            return await ui.respond(interaction, ui.warn("Tournament Full", "This tournament has reached its player limit."), ephemeral=True)
        await interaction.response.defer()
        e["players"].append(uid)
        save()
        await interaction.edit_original_response(embed=make_embed(self.kind, e))

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.red)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        message = interaction.message
        if message is None:
            return await ui.respond(interaction, ui.warn("Invalid Message", "This action must be used from a message."), ephemeral=True)
        e = find_by_message(self.kind, message.id)
        if not e or e["status"] != "open":
            return await ui.respond(interaction, ui.warn("Action Unavailable", "You can no longer leave this event."), ephemeral=True)
        if interaction.user.id not in e["players"]:
            return await ui.respond(interaction, ui.warn("Not Registered", "You have not joined this event."), ephemeral=True)
        await interaction.response.defer()
        e["players"].remove(interaction.user.id)
        save()
        await interaction.edit_original_response(embed=make_embed(self.kind, e))


class CarMeetRegisterModal(discord.ui.Modal, title="Car Meet Registration"):
    name = discord.ui.TextInput(label="Your name / in-game name", max_length=50)
    car = discord.ui.TextInput(label="Car (make & model)", max_length=80)
    mods = discord.ui.TextInput(
        label="Modifications / details",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    plate = discord.ui.TextInput(label="Number plate (optional)", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "Use this form inside a server."), ephemeral=True)
        settings = data["settings"].get(str(guild.id), {})
        review_channel_id = settings.get("review_channel_id", REVIEW_CHANNEL_ID)
        if not review_channel_id:
            return await ui.respond(interaction, ui.error("Setup Incomplete", "The review channel is not configured. Please contact a server administrator."), ephemeral=True)

        applications = data["carmeet_applications"]
        if any(
            application["user_id"] == interaction.user.id
            and application["guild_id"] == guild.id
            and application["status"] == "pending"
            for application in applications.values()
        ):
            return await ui.respond(interaction, ui.warn("Application Pending", "You already have an application awaiting review. Please wait for the staff decision."), ephemeral=True)

        channel = guild.get_channel(review_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await ui.respond(interaction, ui.error("Setup Incomplete", "The review channel could not be found. Please check its channel ID in the server setup."), ephemeral=True)

        application_id = new_id()
        application = {
            "user_id": interaction.user.id,
            "guild_id": guild.id,
            "status": "pending",
            "name": self.name.value,
            "car": self.car.value,
            "mods": self.mods.value,
            "plate": self.plate.value,
        }
        embed = discord.Embed(title="Nayi Car Meet Application", color=discord.Color.orange())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Applicant", value=interaction.user.mention, inline=False)
        embed.add_field(name="Naam", value=application["name"], inline=True)
        embed.add_field(name="Car", value=application["car"], inline=True)
        embed.add_field(name="Plate", value=application["plate"] or "-", inline=True)
        embed.add_field(name="Mods", value=application["mods"] or "-", inline=False)
        embed.set_footer(text=f"Application ID: {application_id}")

        await interaction.response.defer(ephemeral=True)
        try:
            message = await channel.send(embed=embed, view=CarMeetReviewView())
        except discord.Forbidden:
            return await ui.respond(interaction, ui.error("Missing Permissions", "I cannot post in the review channel. Please check my channel permissions."), ephemeral=True)
        application["message_id"] = message.id
        applications[application_id] = application
        save()
        await ui.respond(interaction, ui.success("Application Submitted", "Your application was sent to the staff team. You will be notified once it is reviewed."), ephemeral=True)


class CarMeetRegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Register for Car Meet",
        style=discord.ButtonStyle.primary,
        emoji=ui.SPIDER,
        custom_id="carmeet:register",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CarMeetRegisterModal())


class CarMeetReviewView(discord.ui.View):
    def __init__(self, *, disabled=False):
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                setattr(child, "disabled", True)

    async def review(self, interaction: discord.Interaction, status: str):
        guild = interaction.guild
        if guild is None:
            return await ui.respond(interaction, ui.warn("Server Only", "Use this action inside a server."), ephemeral=True)
        reviewer = interaction.user
        if not isinstance(reviewer, discord.Member):
            return await ui.respond(interaction, ui.error("Invalid Member", "This action can only be used by a guild member."), ephemeral=True)
        settings = data["settings"].get(str(guild.id), {})
        staff_role_id = settings.get("staff_role_id", STAFF_ROLE_ID)
        is_staff = reviewer.guild_permissions.administrator or any(
            role.id == staff_role_id for role in reviewer.roles
        )
        if not is_staff:
            return await ui.respond(interaction, ui.error("Access Denied", "You do not have permission to review applications."), ephemeral=True)

        message = interaction.message
        if message is None:
            return await ui.respond(interaction, ui.error("Invalid Context", "This review action must be triggered from a message."), ephemeral=True)
        try:
            if not message.embeds:
                raise ValueError("No embed")
            footer = message.embeds[0].footer
            if footer is None or footer.text is None:
                raise ValueError("Missing footer")
            application_id = footer.text.split(":", 1)[1].strip()
            application = data["carmeet_applications"][application_id]
        except (AttributeError, IndexError, KeyError, ValueError):
            return await ui.respond(interaction, ui.error("Application Not Found", "The application record could not be found."), ephemeral=True)
        if application["status"] != "pending":
            return await ui.respond(interaction, ui.warn("Already Reviewed", "This application has already been reviewed."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        application["status"] = status
        application["reviewed_by"] = reviewer.id
        save()
        embed = message.embeds[0]
        embed.color = discord.Color.green() if status == "accepted" else discord.Color.red()
        embed.add_field(name="Status", value=f"{status.upper()} by {reviewer.mention}", inline=False)
        await message.edit(embed=embed, view=CarMeetReviewView(disabled=True))

        member = guild.get_member(application["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(application["user_id"])
            except discord.HTTPException:
                pass
        accepted_role_id = settings.get("accepted_role_id", ACCEPTED_ROLE_ID)
        if status == "accepted" and accepted_role_id and member:
            role = guild.get_role(accepted_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Car meet application accepted")
                except discord.Forbidden:
                    await ui.respond(interaction, ui.warn("Role Not Assigned", "The application was accepted, but I could not assign the role. Please check my **Manage Roles** permission."), ephemeral=True)
        if member:
            try:
                await member.send(
                    embed=build_notice_dm(
                        title="Car meet application reviewed",
                        message=(
                            "Your car meet registration has been accepted!"
                            if status == "accepted"
                            else "Your car meet registration was not accepted this time."
                        ),
                        emojis=moderation_emojis_for(bot, guild.id),
                        icon_key="GAVEL" if status == "accepted" else "SHIELD",
                        color=0x23A55A if status == "accepted" else 0xF23F43,
                        bot_avatar_url=bot.user.display_avatar.url if bot.user else None,
                    )
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Car meet decision DM failed for {application['user_id']}: {type(error).__name__}: {error}")
        await ui.respond(interaction, ui.success("Application Reviewed", f"The application was **{status}**."), ephemeral=True)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji=ui.SPIDER, custom_id="carmeet:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.review(interaction, "accepted")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji=ui.SPIDER, custom_id="carmeet:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.review(interaction, "rejected")


def resolve_prefix(bot, message):
    if message.author.id in data.get("noprefix", []):
        return [".", ""]
    return "."


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.invites = True
        super().__init__(
            command_prefix=resolve_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            max_messages=200,
        )
        self.lavalink_uri = LAVALINK_URI
        self.lavalink_password = LAVALINK_PASSWORD
        self.loop_lag_avg_ms = 0.0
        self.loop_lag_max_ms = 0.0
        self.dashboard_api = None
        self.state = data
        self.save = save
        self.mass_mention_whitelist_role_ids = CONFIG.mass_mention_whitelist_role_ids
        self.mass_mention_decay_hours = CONFIG.mass_mention_decay_hours
        self.mass_mention_live_animation = CONFIG.mass_mention_live_animation
        self.link_filter_config = LinkFilterConfig(
            whitelisted_role_ids=frozenset(CONFIG.link_filter_whitelisted_role_ids),
            whitelisted_channel_ids=frozenset(CONFIG.link_filter_whitelisted_channel_ids),
            whitelisted_domains=CONFIG.link_filter_whitelisted_domains,
        )
        self.link_filter_delete_delay = CONFIG.link_filter_delete_delay
        self.link_filter_log_channel_id = CONFIG.link_filter_log_channel_id

    async def setup_hook(self):
        self.add_view(JoinView("event"))
        self.add_view(JoinView("tournament"))
        self.add_view(CarMeetRegisterView())
        self.add_view(CarMeetReviewView())
        for label, step in self._cog_steps():
            try:
                await step()
            except Exception as error:
                print(f"setup_hook: {label} failed to load: {type(error).__name__}: {error}")

        dashboard_config = DashboardConfig.from_env()
        if dashboard_config is not None:
            self.dashboard_api = DashboardAPI(self, data, save, has_tier, dashboard_config)
            await self.dashboard_api.start()

        try:
            await ui.setup_emojis(self, data)
        except Exception as e:
            print(f"setup_emojis failed: {e}")
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global slash command roots.")
        except Exception as error:
            print(f"Slash command sync failed: {type(error).__name__}: {error}")
        gc.collect()
        gc.freeze()
        asyncio.get_running_loop().set_exception_handler(self._loop_error)
        for name, task in (("state_writer", state_writer_loop()), ("loop_lag", loop_lag_loop())):
            setattr(self, f"{name}_task", asyncio.create_task(task))

    def _loop_error(self, loop, context):
        error = context.get("exception")
        if error is None:
            error = RuntimeError(context.get("message", "asyncio error"))
        log_exception(error, context="asyncio task")

    def _cog_steps(self):
        steps = [
            ("LoggingCog", lambda: self.add_cog(LoggingCog(self, data, has_tier, LOG_CHANNEL_ID))),
            ("SecurityCog", lambda: self.add_cog(SecurityCog(
                self, data, save, has_tier, CONFIG.trusted_role_management_bot_ids,
                CONFIG.global_protected_owner_ids,
                CONFIG.promo_image_warning_delete_delay,
            ))),
            ("MassMentionCog", lambda: self.add_cog(MassMentionCog(
                self,
                data,
                save,
                CONFIG.mass_mention_whitelist_role_ids,
                CONFIG.mass_mention_decay_hours,
                CONFIG.mass_mention_live_animation,
            ))),
            ("LinkFilterCog", lambda: self.add_cog(LinkFilterCog(
                self,
                self.link_filter_config,
                self.link_filter_delete_delay,
                self.link_filter_log_channel_id,
            ))),
            ("LogSuite", lambda: self.add_cog(LogSuite(self, data, save, has_tier))),
            ("RoleConfigCog", lambda: self.add_cog(RoleConfigCog(self))),
            ("ModerationCog", lambda: self.add_cog(ModerationCog(self, data, save))),
            ("SmartModCog", lambda: self.add_cog(SmartModCog(self, data, save))),
            ("OwnerHub", lambda: self.add_cog(OwnerHub(self))),
            ("VoiceToolsCog", lambda: self.add_cog(VoiceToolsCog(self))),
            ("VCMoveCog", lambda: self.add_cog(VCMoveCog(self))),
            ("AutoRoleCog", lambda: self.add_cog(AutoRoleCog(self, data, save))),
            ("WelcomerCog", lambda: self.add_cog(WelcomerCog(self, data, save))),
            ("InviteTrackerCog", lambda: self.add_cog(InviteTrackerCog(self, data, save))),
            ("TicketCog", lambda: self.add_cog(TicketCog(self, data, save))),
            ("JailCog", lambda: self.add_cog(JailCog(self, data, save))),
            ("SelfRolesCog", lambda: self.add_cog(SelfRolesCog(self, data, save))),
            ("JoinToCreateCog", lambda: self.add_cog(JoinToCreateCog(self, data, save))),
            ("UtilityCog", lambda: self.add_cog(UtilityCog(self))),
            ("OtherCog", lambda: self.add_cog(OtherCog(self))),
            ("NoPrefixCog", lambda: self.add_cog(NoPrefixCog(self, data, save))),
            ("HelpCog", lambda: self.add_cog(HelpCog(self))),
            ("JokesCog", lambda: self.add_cog(JokesCog(self, data, save))),
        ]
        if CONFIG.music_enabled:
            steps.insert(3, ("music_panel", lambda: self.load_extension("music_panel")))
        else:
            print("Music extension disabled by MUSIC_ENABLED=false.")
        return tuple(steps)

    async def close(self):
        try:
            if self.dashboard_api is not None:
                await self.dashboard_api.close()
        finally:
            await flush_state()
            STORE.close()
            gc.unfreeze()
            await super().close()


bot = Bot()


def get_log_channel(guild):
    cog = bot.get_cog("LoggingCog")
    if cog is None:
        return None
    return cast(Any, cog).get_log_channel(guild)


def get_automod_settings(guild_id):
    settings = data["settings"].get(str(guild_id), {})
    return settings.setdefault(
        "automod",
        {"enabled": False, "message_limit": 5, "window_seconds": 8, "mention_limit": 5, "invite_filter": False},
    )


async def write_log(guild, embed):
    cog = bot.get_cog("LoggingCog")
    if cog is not None:
        await cast(Any, cog).write_log(guild, embed)


@bot.command(name="announce")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def announce(ctx, *, message: str):
    ping_tokens = list(dict.fromkeys(re.findall(r"@(everyone|here)\b", message, re.IGNORECASE)))
    permissions = ctx.channel.permissions_for(ctx.guild.me)
    if ping_tokens and not permissions.mention_everyone:
        await ctx.send(embed=ui.warn("Missing Permissions", "I need Mention Everyone permission to ping @everyone or @here."))
        return
    description = re.sub(r"@(everyone|here)\b", "", message, flags=re.IGNORECASE).strip()
    embed = ui.announcement(
        ctx.guild,
        "Server Announcement",
        description or None,
    )
    await ctx.send(
        content=" ".join(f"@{token.lower()}" for token in ping_tokens) or None,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=True),
    )


@bot.command(name="syncslash")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def sync_slash_commands(ctx):
    bot.tree.clear_commands(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    global_synced = await bot.tree.sync()
    await ctx.send(
        embed=ui.success(
            "Slash Commands Refreshed",
            f"Guild commands refreshed: {len(synced)}.\n"
            f"Global command roots re-synced: {len(global_synced)}.\n"
            "If /music247 still does not appear, restart the Discord "
            "app or wait a few minutes.",
        )
    )


PLAN_NICKNAMES = {"prime": "KRYO PRIME"}
PLAN_LABELS = {"prime": "PRIME", "free": "Free"}
PRIME_ROLE_NAME = "KRYO PRIME"
PRIME_ROLE_COLOR = 0xD4A50B


def plan_label(guild_id):
    tier = active_tier(guild_id)
    if tier == "free":
        return "Free"
    stored = data["subscriptions"].get(str(guild_id), {}).get("tier")
    return PLAN_LABELS.get(stored, tier.title())


async def sync_subscription_nickname(guild, subscription):
    me = guild.me
    if me is None:
        return False
    tier = active_tier(guild.id)
    if tier == "free":
        target_nick = subscription.get("original_nick")
    else:
        target_nick = PLAN_NICKNAMES.get(tier, "KRYO PRIME")
    if me.nick != target_nick:
        await me.edit(nick=target_nick, reason="Subscription badge sync")
    if not await sync_subscription_color_role(guild, subscription, tier):
        return False
    subscription["nickname_synced_tier"] = tier
    save()
    return True


async def sync_subscription_color_role(guild, subscription, tier):
    me = guild.me
    if me is None:
        return False
    try:
        role_id = int(subscription.get("prime_color_role_id", 0) or 0)
    except (TypeError, ValueError):
        role_id = 0
    role = guild.get_role(role_id) if role_id else None

    if tier == "free":
        if role is not None and role in me.roles:
            await me.remove_roles(role, reason="Remove KRYO Prime color after subscription ends")
        return True

    if not me.guild_permissions.manage_roles:
        return False
    if role is None:
        role = await guild.create_role(
            name=PRIME_ROLE_NAME,
            colour=discord.Colour(PRIME_ROLE_COLOR),
            permissions=discord.Permissions.none(),
            hoist=False,
            mentionable=False,
            reason="Create KRYO Prime badge color role",
        )
        subscription["prime_color_role_id"] = role.id
        save()

    position = max(1, me.top_role.position - 1)
    if role.position != position:
        await guild.edit_role_positions(
            positions={role: position},
            reason="Display KRYO Prime badge color",
        )
    if role.colour.value != PRIME_ROLE_COLOR:
        await role.edit(
            colour=discord.Colour(PRIME_ROLE_COLOR),
            reason="Set KRYO Prime badge color",
        )
    if role not in me.roles:
        await me.add_roles(role, reason="Apply KRYO Prime badge color")
    return True


@bot.group(name="prime", invoke_without_command=True)
@commands.guild_only()
async def prime(ctx):
    tier = active_tier(ctx.guild.id)
    subscription = data["subscriptions"].get(str(ctx.guild.id), {})
    embed = discord.Embed(
        title=f"{ui.SPIDER} Server Plan",
        description=f"**{ctx.guild.name}** is on the **{plan_label(ctx.guild.id)}** plan.",
        color=discord.Color.gold() if tier != "free" else discord.Color.dark_grey(),
    )
    if tier != "free":
        try:
            expiry = datetime.fromisoformat(subscription["expires_at"]).astimezone(timezone.utc)
            embed.add_field(name="Active until", value=f"{expiry:%Y-%m-%d %H:%M UTC}", inline=False)
        except (KeyError, TypeError, ValueError):
            pass
    embed.set_footer(text="KRYO • Run .prime tiers to compare plans.")
    await ctx.send(embed=embed)


@prime.command(name="tiers")
async def prime_tiers(ctx):
    embed = discord.Embed(title=f"{ui.SPIDER} KRYO Plans", color=discord.Color(0x8A2BE2))
    embed.add_field(
        name="Free",
        value="Events, tournaments, music and moderation commands.",
        inline=False,
    )
    embed.add_field(
        name=f"PRIME · ₹{PREMIUM_TIERS['prime']}/month",
        value=(
            "Antinuke protection, anti raid, anti spam, whitelist, security logs, "
            "voice channel lock and vanity URL change alerts.\n"
            "Double protection stays on and antinuke works faster."
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@prime.command(name="grant")
@commands.is_owner()
async def prime_grant(ctx, guild_id: int, tier: str, days: int = 30):
    tier = tier.lower()
    if tier not in PREMIUM_TIERS:
        return await ctx.send(embed=ui.warn("Invalid Plan", "Choose one of: " + ", ".join(f"`{name}`" for name in sorted(PREMIUM_TIERS)) + "."))
    guild = bot.get_guild(guild_id)
    if guild is None:
        return await ctx.send(
            embed=ui.error("Server Not Found", "I am not in that server. Invite the bot there first, then run this command again.")
        )
    if not 1 <= days <= 366:
        return await ctx.send(embed=ui.warn("Invalid Duration", "Duration must be between **1** and **366** days."))
    now = datetime.now(timezone.utc)
    previous = data["subscriptions"].get(str(guild_id), {})
    bot_member = guild.me
    data["subscriptions"][str(guild_id)] = {
        "tier": tier,
        "expires_at": (now + timedelta(days=days)).isoformat(),
        "granted_by": ctx.author.id,
        "original_nick": previous.get(
            "original_nick", bot_member.nick if bot_member is not None else None
        ),
        "nickname_synced_tier": previous.get("nickname_synced_tier"),
        "prime_color_role_id": previous.get("prime_color_role_id"),
    }
    _invalidate_tier_cache(guild_id)
    save()
    subscription = data["subscriptions"][str(guild_id)]
    try:
        badge_updated = await sync_subscription_nickname(guild, subscription)
    except (discord.Forbidden, discord.HTTPException):
        badge_updated = False
    expiry = datetime.fromisoformat(subscription["expires_at"]).astimezone(timezone.utc)
    embed = discord.Embed(
        title=f"{PREMIUM_TICK_EMOJI} {PLAN_LABELS[tier].upper()} ACTIVATED",
        description=f"**{guild.name}** is now on the **{PLAN_LABELS[tier]}** plan. All protections are unlocked.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Plan", value=f"{PLAN_LABELS[tier]} · ₹{PREMIUM_TIERS[tier]}/month", inline=True)
    embed.add_field(name="Active until", value=f"{expiry:%Y-%m-%d %H:%M UTC}", inline=True)
    if badge_updated:
        embed.add_field(name="Server badge", value=f"{guild.me.display_name} · #{PRIME_ROLE_COLOR:06X}", inline=False)
    else:
        embed.add_field(
            name="Server badge",
            value="Could not update the bot nickname or Prime color role. Grant Change Nickname and Manage Roles permissions.",
            inline=False,
        )
    embed.set_footer(text="Thank you for choosing KRYO")
    await ctx.send(embed=embed)


@prime.command(name="revoke")
@commands.is_owner()
async def prime_revoke(ctx, guild_id: int):
    subscription = data["subscriptions"].get(str(guild_id))
    if subscription is None:
        return await ctx.send(embed=ui.warn("No Subscription", "No subscription record was found for that server."))
    subscription["expires_at"] = datetime.now(timezone.utc).isoformat()
    subscription["revoked_by"] = ctx.author.id
    _invalidate_tier_cache(guild_id)
    save()
    guild = bot.get_guild(guild_id)
    badge_restored = False
    if guild is not None:
        try:
            badge_restored = await sync_subscription_nickname(guild, subscription)
        except discord.Forbidden:
            pass
    suffix = "" if badge_restored else " The original nickname or Prime color role could not be restored; check Change Nickname and Manage Roles permissions."
    await ctx.send(embed=ui.success("Subscription Revoked", f"PRIME access was revoked for server `{guild_id}`.{suffix}"))


async def subscription_expiry_watcher():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild_id, subscription in list(data["subscriptions"].items()):
                _invalidate_tier_cache(guild_id)
                if "original_nick" not in subscription:
                    continue
                guild = bot.get_guild(int(guild_id))
                if guild is None:
                    continue
                tier = active_tier(int(guild_id))
                try:
                    role_id = int(subscription.get("prime_color_role_id", 0) or 0)
                except (TypeError, ValueError):
                    role_id = 0
                color_role = guild.get_role(role_id) if role_id else None
                color_role_assigned = color_role is not None and color_role in guild.me.roles
                if subscription.get("nickname_synced_tier") == tier and (
                    (tier == "free" and not color_role_assigned)
                    or (tier != "free" and color_role_assigned and color_role.colour.value == PRIME_ROLE_COLOR)
                ):
                    continue
                try:
                    await sync_subscription_nickname(guild, subscription)
                except (discord.Forbidden, discord.HTTPException):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[prime] expiry sweep failed: {type(error).__name__}: {error}")
        await asyncio.sleep(60)


vc_group = app_commands.Group(name="vc", description="Voice channel controls")


@vc_group.command(name="lock", description="Lock a voice channel for @everyone")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vc_lock(interaction: discord.Interaction, channel: discord.VoiceChannel):
    guild = interaction.guild
    if guild is None:
        return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
    if not has_tier(interaction.guild_id, "prime"):
        return await ui.respond(interaction, ui.error("Plan Required", "Voice channel locking requires an active **PRIME** plan."), ephemeral=True)
    settings = data["settings"].setdefault(str(interaction.guild_id), {})
    locks = settings.setdefault("vc_locks", {})
    channel_key = str(channel.id)
    if channel_key in locks:
        return await ui.respond(interaction, ui.warn("Already Locked", "This channel is already locked."), ephemeral=True)

    overwrite = channel.overwrites_for(guild.default_role)
    locks[channel_key] = {"connect": overwrite.connect}
    overwrite.connect = False
    try:
        await channel.set_permissions(
            guild.default_role,
            overwrite=overwrite,
            reason=f"Voice channel locked by {interaction.user}",
        )
    except discord.Forbidden:
        locks.pop(channel_key, None)
        return await ui.respond(interaction, ui.error("Missing Permissions", "I need the **Manage Channels** permission to do that."), ephemeral=True)
    save()
    await ui.respond(interaction, ui.success("Channel Locked", f"{channel.mention} is now locked."), ephemeral=True)


@vc_group.command(name="unlock", description="Unlock a voice channel locked by this bot")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def vc_unlock(interaction: discord.Interaction, channel: discord.VoiceChannel):
    guild = interaction.guild
    if guild is None:
        return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
    settings = data["settings"].setdefault(str(interaction.guild_id), {})
    locks = settings.setdefault("vc_locks", {})
    channel_key = str(channel.id)
    record = locks.get(channel_key)
    if record is None:
        return await ui.respond(interaction, ui.warn("Not Locked", "This channel has no lock saved by the bot."), ephemeral=True)

    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.connect = record.get("connect")
    try:
        await channel.set_permissions(
            guild.default_role,
            overwrite=overwrite,
            reason=f"Voice channel unlocked by {interaction.user}",
        )
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "I need the **Manage Channels** permission to do that."), ephemeral=True)
    locks.pop(channel_key)
    save()
    await ui.respond(interaction, ui.success("Channel Unlocked", f"{channel.mention} is now unlocked."), ephemeral=True)


bot.tree.add_command(vc_group)

moderation_group = app_commands.Group(name="moderation", description="Common server moderation tools")


def target_is_actionable(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    if guild is None or member.id == guild.owner_id:
        return False
    requester = interaction.user
    if not isinstance(requester, discord.Member):
        return False
    if requester.id != guild.owner_id and member.top_role >= requester.top_role:
        return False
    bot_member = guild.me
    return bot_member is not None and member.top_role < bot_member.top_role


@moderation_group.command(name="ban", description="Ban a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def moderation_ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    delete_message_days: app_commands.Range[int, 0, 7] = 0,
):
    guild = interaction.guild
    if guild is None:
        return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
    if not target_is_actionable(interaction, member):
        return await ui.respond(interaction, ui.error("Action Blocked", "My role must be above this member's highest role to ban them."), ephemeral=True)
    await try_dm(
        member,
        action_embed(
            guild, "banned", reason, interaction.user,
            moderation_emojis_for(bot, guild.id),
            bot.user.display_avatar.url if bot.user else None,
        ),
    )
    try:
        await guild.ban(
            member,
            delete_message_seconds=delete_message_days * 86400,
            reason=reason,
        )
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "Please check my **Ban Members** permission and my role position."), ephemeral=True)
    await ui.respond(interaction, ui.success("Member Banned", f"**{member}** was banned.\n**Reason:** {reason}"), ephemeral=True)


@moderation_group.command(name="unban", description="Unban a user by selecting them")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def moderation_unban(
    interaction: discord.Interaction,
    user: discord.User,
    reason: str = "No reason provided",
):
    guild = interaction.guild
    if guild is None:
        return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
    try:
        await guild.unban(user, reason=reason)
    except discord.NotFound:
        return await ui.respond(interaction, ui.warn("Not Banned", "This user is not banned."), ephemeral=True)
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "I need the **Ban Members** permission to do that."), ephemeral=True)
    await try_dm(
        user,
        action_embed(
            guild, "unbanned", reason, interaction.user,
            moderation_emojis_for(bot, guild.id),
            bot.user.display_avatar.url if bot.user else None,
        ),
    )
    await ui.respond(interaction, ui.success("Member Unbanned", f"**{user}** was unbanned."), ephemeral=True)


@moderation_group.command(name="kick", description="Kick a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
async def moderation_kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if not target_is_actionable(interaction, member):
        return await ui.respond(interaction, ui.error("Action Blocked", "My role must be above this member's highest role to kick them."), ephemeral=True)
    await try_dm(
        member,
        action_embed(
            interaction.guild, "kicked", reason, interaction.user,
            moderation_emojis_for(bot, interaction.guild.id),
            bot.user.display_avatar.url if bot.user else None,
        ),
    )
    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "Please check my **Kick Members** permission and my role position."), ephemeral=True)
    await ui.respond(interaction, ui.success("Member Kicked", f"**{member}** was kicked.\n**Reason:** {reason}"), ephemeral=True)


@moderation_group.command(name="timeout", description="Timeout a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def moderation_timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided",
):
    if not target_is_actionable(interaction, member):
        return await ui.respond(interaction, ui.error("Action Blocked", "My role must be above this member's highest role to time them out."), ephemeral=True)
    await try_dm(
        member,
        action_embed(
            interaction.guild, "timed out", f"{reason} ({minutes} minutes)", interaction.user,
            moderation_emojis_for(bot, interaction.guild.id),
            bot.user.display_avatar.url if bot.user else None,
        ),
    )
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "Please check my **Moderate Members** permission and my role position."), ephemeral=True)
    await ui.respond(interaction, ui.success("Member Timed Out", f"{member.mention} was timed out for **{minutes}** minute(s)."), ephemeral=True)


@moderation_group.command(name="untimeout", description="Remove a member timeout")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def moderation_untimeout(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if not target_is_actionable(interaction, member):
        return await ui.respond(interaction, ui.error("Action Blocked", "My role must be above this member's highest role to remove their timeout."), ephemeral=True)
    try:
        await member.timeout(None, reason=reason)
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "Please check my **Moderate Members** permission and my role position."), ephemeral=True)
    await ui.respond(interaction, ui.success("Timeout Removed", f"The timeout on {member.mention} was removed."), ephemeral=True)


@moderation_group.command(name="warn", description="Add a moderation warning")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def moderation_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    guild = interaction.guild
    if guild is None:
        return await ui.respond(interaction, ui.warn("Server Only", "This command can only be used in a server."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    key = f"{interaction.guild_id}:{member.id}"
    warnings = data["warnings"].setdefault(key, [])
    warnings.append({
        "reason": reason,
        "moderator_id": interaction.user.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save()
    await try_dm(
        member,
        build_warning_dm(
            guild_name=guild.name,
            reason=reason,
            total_warnings=len(warnings),
            emojis=moderation_emojis_for(bot, guild.id),
            bot_avatar_url=bot.user.display_avatar.url if bot.user else None,
        ),
    )
    smart = bot.get_cog("Smart Moderation")
    smart_obj = cast(Any, smart)
    note = await smart_obj.apply(guild, member, len(warnings)) if smart_obj is not None else None
    text = f"{member.mention} received a warning.\n**Total warnings:** {len(warnings)}"
    if note:
        text += f"\n\n> **{note}**"
    await ui.respond(interaction, ui.warn("Member Warned", text), ephemeral=True)


@moderation_group.command(name="warnings", description="Show a member's recent warnings")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def moderation_warnings(interaction: discord.Interaction, member: discord.Member):
    warnings = data["warnings"].get(f"{interaction.guild_id}:{member.id}", [])
    if not warnings:
        return await ui.respond(interaction, ui.info("No Warnings", "This member has no warnings."), ephemeral=True)
    lines = [
        f"{index}. {warning['reason'][:150]} (by <@{warning['moderator_id']}>)"
        for index, warning in enumerate(warnings[-10:], start=max(1, len(warnings) - 9))
    ]
    await ui.respond(interaction, ui.info("Member Warnings", "\n".join(lines)), ephemeral=True)


@moderation_group.command(name="clear_warnings", description="Clear all warnings for a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def moderation_clear_warnings(interaction: discord.Interaction, member: discord.Member):
    removed = data["warnings"].pop(f"{interaction.guild_id}:{member.id}", None)
    if removed is None:
        return await ui.respond(interaction, ui.info("No Warnings", "This member has no warnings."), ephemeral=True)
    save()
    await ui.respond(interaction, ui.success("Warnings Cleared", f"All warnings for {member.mention} were cleared."), ephemeral=True)


@moderation_group.command(name="purge", description="Delete recent messages from this channel")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def moderation_purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await ui.respond(interaction, ui.warn("Text Channel Only", "Use this command in a text channel."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount, reason=f"Bulk purge by {interaction.user}")
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "I need the **Manage Messages** permission to do that."), ephemeral=True)
    await ui.respond(interaction, ui.success("Messages Deleted", f"**{len(deleted)}** messages were deleted."), ephemeral=True)


@moderation_group.command(name="slowmode", description="Set a text channel slowmode")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def moderation_slowmode(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    seconds: app_commands.Range[int, 0, 21600],
):
    try:
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
    except discord.Forbidden:
        return await ui.respond(interaction, ui.error("Missing Permissions", "I need the **Manage Channels** permission to do that."), ephemeral=True)
    await ui.respond(interaction, ui.success("Slowmode Updated", f"Slowmode in {channel.mention} is now **{seconds}s**."), ephemeral=True)


bot.tree.add_command(moderation_group)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.MissingRole, app_commands.MissingAnyRole)):
        emb = ui.error("Access Denied", "You do not have the permissions required to use this command.")
    elif isinstance(error, app_commands.NoPrivateMessage):
        emb = ui.warn("Server Only", "This command can only be used inside a server.")
    elif isinstance(error, app_commands.CommandOnCooldown):
        emb = ui.warn("Slow Down", f"Try again in **{error.retry_after:.1f}s**.")
    elif isinstance(error, app_commands.CheckFailure):
        emb = ui.error("Not Allowed", str(error) or "You cannot use this command.")
    else:
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        log_exception(
            error,
            context=f"slash /{interaction.command.qualified_name if interaction.command else '?'} guild={interaction.guild_id}",
        )
        emb = ui.error("Something Went Wrong", "The command could not be completed. The issue was logged for the developer.")
    try:
        if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
            await interaction.edit_original_response(embed=emb)
        else:
            await ui.respond(interaction, emb, ephemeral=True)
    except discord.HTTPException:
        pass


admin = app_commands.checks.has_permissions(manage_events=True)


@bot.tree.command(name="carmeet_panel", description="Post the car meet registration panel")
@admin
async def carmeet_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Car Meet Registration",
        description="Press the button below to register for the meet and submit your car details.",
        color=discord.Color.orange(),
    )
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await ui.respond(interaction, ui.warn("Invalid Channel", "This command must be used in a text channel."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await channel.send(embed=embed, view=CarMeetRegisterView())
    await ui.respond(interaction, ui.success("Panel Posted", "The registration panel has been posted."), ephemeral=True)


@bot.tree.command(name="event_create", description="Create a new event")
@admin
async def event_create(interaction: discord.Interaction, name: str, description: str = ""):
    e = {
        "id": new_id(), "name": name, "desc": description, "status": "open",
        "players": [], "host_name": interaction.user.display_name,
        "channel_id": interaction.channel_id, "message_id": None,
    }
    data["event"][e["id"]] = e
    await interaction.response.defer()
    message = await interaction.edit_original_response(embed=make_embed("event", e), view=JoinView("event"))
    e["message_id"] = message.id
    save()


@bot.tree.command(name="event_start", description="Start an event and notify its participants")
@admin
async def event_start(interaction: discord.Interaction, event_id: str, image_url: str = ""):
    e = data["event"].get(event_id)
    if not e:
        return await ui.respond(interaction, ui.error("Invalid Event", "No event was found with that ID."), ephemeral=True)
    image_url = image_url.strip()
    if image_url:
        try:
            parsed_url = urlsplit(image_url)
            valid_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)
        except ValueError:
            valid_url = False
        if not valid_url:
            return await ui.respond(interaction, ui.warn("Invalid Image URL", "Provide a valid HTTP or HTTPS image URL."), ephemeral=True)
    await interaction.response.defer()
    e["status"] = "live"
    save()
    pings = " ".join(f"<@{u}>" for u in e["players"]) or "No one has joined yet."
    embed = discord.Embed(
        title=f"{ui.SPIDER} {e['name']} has started!",
        description=pings,
        color=discord.Color.orange(),
    )
    if image_url:
        embed.set_thumbnail(url=image_url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="event_end", description="End an event")
@admin
async def event_end(interaction: discord.Interaction, event_id: str):
    e = data["event"].get(event_id)
    if not e:
        return await ui.respond(interaction, ui.error("Invalid Event", "No event was found with that ID."), ephemeral=True)
    await interaction.response.defer()
    e["status"] = "ended"
    save()
    await ui.respond(interaction, ui.success("Event Ended", f"**{e['name']}** has ended. Thank you to everyone who joined!"))


def make_round(players, shuffle=False):
    players = list(players)
    if shuffle:
        random.shuffle(players)
    matches = []
    for i in range(0, len(players), 2):
        p1 = players[i]
        p2 = players[i + 1] if i + 1 < len(players) else None
        matches.append([p1, p2, p1 if p2 is None else None])
    return matches


@bot.tree.command(name="tournament_create", description="Create a new tournament")
@admin
async def tournament_create(interaction: discord.Interaction, name: str,
                            max_players: app_commands.Range[int, 2, 128] = 16,
                            description: str = ""):
    t = {
        "id": new_id(), "name": name, "desc": description, "status": "open",
        "max": max_players, "players": [], "rounds": [],
        "host_name": interaction.user.display_name,
        "channel_id": interaction.channel_id, "message_id": None,
    }
    data["tournament"][t["id"]] = t
    await interaction.response.defer()
    message = await interaction.edit_original_response(
        embed=make_embed("tournament", t), view=JoinView("tournament")
    )
    t["message_id"] = message.id
    save()


@bot.tree.command(name="tournament_start", description="Close registration and generate the bracket")
@admin
async def tournament_start(interaction: discord.Interaction, tournament_id: str):
    t = data["tournament"].get(tournament_id)
    if not t:
        return await ui.respond(interaction, ui.error("Invalid Tournament", "No tournament was found with that ID."), ephemeral=True)
    if t["status"] != "open":
        return await ui.respond(interaction, ui.warn("Already Started", "This tournament has already started."), ephemeral=True)
    if len(t["players"]) < 2:
        return await ui.respond(interaction, ui.warn("Not Enough Players", "At least **2** players are required to start."), ephemeral=True)
    await interaction.response.defer()
    t["status"] = "live"
    t["rounds"] = [make_round(t["players"], shuffle=True)]
    save()
    await ui.respond(interaction, ui.success("Tournament Started", f"**{t['name']}** has started!\n\n{bracket_text(t)}"))


@bot.tree.command(name="tournament_report", description="Report the winner of a match")
@admin
async def tournament_report(interaction: discord.Interaction, tournament_id: str,
                            match_no: int, winner: discord.Member):
    t = data["tournament"].get(tournament_id)
    if not t or t["status"] != "live":
        return await ui.respond(interaction, ui.error("No Live Tournament", "No live tournament was found with that ID."), ephemeral=True)
    matches = t["rounds"][-1]
    if not 1 <= match_no <= len(matches):
        return await ui.respond(interaction, ui.warn("Invalid Match", "That match number does not exist in the current round."), ephemeral=True)
    m = matches[match_no - 1]
    if winner.id not in (m[0], m[1]):
        return await ui.respond(interaction, ui.warn("Wrong Player", "That player is not part of this match."), ephemeral=True)
    await interaction.response.defer()
    m[2] = winner.id

    msg = f"{ui.SPIDER} **Match #{match_no}** — {winner.mention} advances."
    if all(x[2] for x in matches):
        winners = [x[2] for x in matches]
        if len(winners) == 1:
            t["status"] = "finished"
            msg += f"\n\n{ui.SPIDER} **{t['name']}** has a champion: {winner.mention}. Congratulations!"
        else:
            t["rounds"].append(make_round(winners))
            msg += f"\n\n**The next round has started.**\n{bracket_text(t)}"
    save()
    await interaction.followup.send(msg)


@bot.tree.command(name="tournament_bracket", description="Show the current tournament bracket")
async def tournament_bracket(interaction: discord.Interaction, tournament_id: str):
    t = data["tournament"].get(tournament_id)
    if not t:
        return await ui.respond(interaction, ui.error("Invalid Tournament", "No tournament was found with that ID."), ephemeral=True)
    await interaction.response.send_message(bracket_text(t))


@bot.event
async def on_ready():
    expiry_task = getattr(bot, "prime_expiry_task", None)
    if expiry_task is None or expiry_task.done():
        setattr(bot, "prime_expiry_task", asyncio.create_task(subscription_expiry_watcher()))
    print(f"Logged in as {bot.user}")
    security_cog = bot.get_cog("Security")
    if security_cog is not None:
        try:
            await cast(Any, security_cog).recover_on_ready()
        except Exception as error:
            print(f"[security] restart recovery failed: {type(error).__name__}: {error}")


@bot.event
async def on_error(event_method, *args, **kwargs):
    error = sys.exc_info()[1]
    if error is not None:
        log_exception(error, context=f"event {event_method}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Set the bot token in D:\\disocrd\\.env.")
    if TOKEN in {"your_discord_bot_token_here", "APNA_NAYA_TOKEN"}:
        raise SystemExit("DISCORD_TOKEN is still a placeholder. Replace it with your bot token.")
    import faulthandler

    faulthandler.enable()

    def _fatal(exc_type, exc_value, exc_traceback):
        if exc_value is not None:
            log_exception(exc_value, context="fatal")
        exception = exc_value if exc_value is not None else RuntimeError("Unknown fatal exception")
        sys.__excepthook__(exc_type or RuntimeError, exception, exc_traceback)

    sys.excepthook = _fatal
    bot.run(TOKEN)
