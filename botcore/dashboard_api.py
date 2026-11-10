from __future__ import annotations
import asyncio
import copy
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit
import aiohttp
import discord
import wavelink
from aiohttp import web
from discord.ext import commands

from cogs.logsuite import LOG_TYPES
from cogs.jail import JailActionError, JailCog
from cogs.security import ROLE_PROTECTION_DEFAULTS, SecurityCog
from cogs.tickets import DEFAULT_CATEGORIES
from cogs.music import build_filters
from botcore.perms import dangerous
from botcore import ui
from botcore.security.rules import (
    DANGEROUS_PERMISSIONS,
    DEFAULT_SPAM_TIMEOUT_MINUTES,
    DEFAULT_TIMEOUT_MINUTES,
    MAX_TIMEOUT_MINUTES,
    MIN_TIMEOUT_MINUTES,
    NUKE_MODULES,
    PROTECTIONS,
    PUNISHABLE_MODULES,
)


SESSION_COOKIE = "kryo_dashboard"
OAUTH_STATE_COOKIE = "kryo_oauth_state"
SESSION_SECONDS = 30 * 24 * 60 * 60
GUILD_ACCESS_CACHE_SECONDS = 30
DISCORD_API = "https://discord.com/api/v10"
ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5


@dataclass(frozen=True)
class DashboardConfig:
    client_id: str
    client_secret: str
    frontend_url: str
    port: int

    @classmethod
    def from_env(cls) -> DashboardConfig | None:
        import os

        names = (
            "DASHBOARD_DISCORD_CLIENT_ID",
            "DASHBOARD_DISCORD_CLIENT_SECRET",
            "DASHBOARD_FRONTEND_URL",
        )
        values = {name: os.getenv(name, "").strip() for name in names}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError("Dashboard setup is incomplete; missing " + ", ".join(missing))
        frontend = urlsplit(values["DASHBOARD_FRONTEND_URL"])
        if (
            frontend.scheme != "https"
            or not frontend.netloc
            or frontend.username
            or frontend.password
            or frontend.path not in ("", "/")
            or frontend.query
            or frontend.fragment
        ):
            raise RuntimeError("DASHBOARD_FRONTEND_URL must be an https origin without a path.")
        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as error:
            raise RuntimeError("PORT must be a valid TCP port.") from error
        if not 1 <= port <= 65535:
            raise RuntimeError("PORT must be between 1 and 65535.")
        return cls(
            client_id=values["DASHBOARD_DISCORD_CLIENT_ID"],
            client_secret=values["DASHBOARD_DISCORD_CLIENT_SECRET"],
            frontend_url=f"{frontend.scheme}://{frontend.netloc}",
            port=port,
        )


@dataclass
class DashboardSession:
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    user: dict[str, Any]
    expires_at: float
    guild_access: list[dict[str, Any]] | None = None
    guild_access_expires_at: float = 0


class DashboardAPI:
    def __init__(
        self,
        bot: commands.Bot,
        state: dict[str, Any],
        save: Callable[[], None],
        has_tier: Callable[[int, str], bool],
        config: DashboardConfig,
    ) -> None:
        self.bot = bot
        self.state = state
        self.save = save
        self.has_tier = has_tier
        self.config = config
        self._sessions: dict[str, DashboardSession] = {}
        self.restore_sessions()
        self._oauth_states: dict[str, float] = {}
        self._http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def restore_sessions(self) -> None:
        now = time.time()
        stored = self.state.get("dashboard_sessions", {})
        if not isinstance(stored, dict):
            return
        for session_id, record in stored.items():
            if not isinstance(session_id, str) or not isinstance(record, dict):
                continue
            access_token = record.get("access_token")
            refresh_token = record.get("refresh_token")
            expires_at = record.get("expires_at")
            access_token_expires_at = record.get("access_token_expires_at")
            user = record.get("user")
            if (
                not isinstance(access_token, str)
                or not isinstance(refresh_token, str)
                or not isinstance(expires_at, (int, float))
                or not isinstance(access_token_expires_at, (int, float))
                or not isinstance(user, dict)
                or expires_at <= now
            ):
                continue
            self._sessions[session_id] = DashboardSession(
                access_token=access_token,
                refresh_token=refresh_token,
                access_token_expires_at=float(access_token_expires_at),
                user=user,
                expires_at=float(expires_at),
            )

    def persist_sessions(self) -> None:
        self.state["dashboard_sessions"] = {
            session_id: {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "access_token_expires_at": session.access_token_expires_at,
                "user": session.user,
                "expires_at": session.expires_at,
            }
            for session_id, session in self._sessions.items()
        }
        self.save()

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent": "KRYO Dashboard"},
        )
        app = web.Application(client_max_size=32 * 1024)
        app.add_routes(
            [
                web.get("/api/auth/login", self.login),
                web.get("/api/auth/callback", self.callback),
                web.get("/api/auth/me", self.me),
                web.post("/api/auth/logout", self.logout),
                web.get("/api/guilds", self.guilds),
                web.get("/api/guilds/{guild_id}/settings", self.get_settings),
                web.put("/api/guilds/{guild_id}/settings", self.update_settings),
                web.post("/api/guilds/{guild_id}/announcements", self.send_announcement),
                web.post("/api/guilds/{guild_id}/tickets/panel", self.publish_ticket_panel),
                web.post("/api/guilds/{guild_id}/selfroles/panel", self.publish_self_roles_panel),
                web.post("/api/guilds/{guild_id}/jail", self.manage_jail),
                web.post("/api/guilds/{guild_id}/music", self.music_action),
                web.options("/api/{tail:.*}", self.options),
                web.options("/{tail:.*}", self.options),
            ]
        )
        app.middlewares.append(self.error_middleware)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.config.port)
        await self._site.start()
        print(f"Dashboard API listening on port {self.config.port}.")

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    @web.middleware
    async def error_middleware(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path != "/api/auth/callback":
            if request.headers.get("Origin") != self.config.frontend_url:
                return self.json_error("Invalid request origin.", 403)
        try:
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        except web.HTTPException as error:
            error.headers["Cache-Control"] = "no-store"
            raise
        except Exception as error:
            print(f"Dashboard API request failed ({type(error).__name__}).")
            return self.json_error("The dashboard request could not be completed.", 500)

    @staticmethod
    def json_error(message: str, status: int) -> web.Response:
        return web.json_response({"error": message}, status=status)

    def callback_url(self) -> str:
        return f"{self.config.frontend_url}/api/auth/callback"

    def require_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            raise RuntimeError("Dashboard API HTTP client is not running.")
        return self._http

    async def login(self, _request: web.Request) -> web.Response:
        state = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._oauth_states = {key: expires for key, expires in self._oauth_states.items() if expires > now}
        self._oauth_states[state] = now + 600
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.callback_url(),
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
            }
        )
        response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            state,
            max_age=600,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/api/auth/callback",
        )
        return response

    async def callback(self, request: web.Request) -> web.Response:
        oauth_error = request.query.get("error")
        if oauth_error:
            response = web.HTTPFound(f"{self.config.frontend_url}/?dashboard_error=discord_login_denied#dashboard")
            response.del_cookie(OAUTH_STATE_COOKIE, path="/api/auth/callback", secure=True, httponly=True, samesite="Lax")
            return response
        state = request.query.get("state", "")
        expires = self._oauth_states.pop(state, 0)
        code = request.query.get("code", "")
        browser_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
        if not state or expires < time.monotonic() or not code or not secrets.compare_digest(state, browser_state):
            response = web.Response(status=400, text="Invalid or expired Discord login request.")
            response.del_cookie(OAUTH_STATE_COOKIE, path="/api/auth/callback", secure=True, httponly=True, samesite="Lax")
            return response
        http = self.require_http()
        async with http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.callback_url(),
            },
        ) as response:
            if response.status != 200:
                print(f"Discord OAuth token exchange failed with status {response.status}.")
                raise web.HTTPBadGateway(text="Discord sign-in could not be completed.")
            token_payload = await response.json()
        access_token = token_payload.get("access_token")
        refresh_token = token_payload.get("refresh_token")
        expires_in = token_payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise web.HTTPBadGateway(text="Discord did not return an access token.")
        async with http.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            if response.status != 200:
                raise web.HTTPBadGateway(text="Could not verify the Discord account.")
            user = await response.json()
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[session_id] = DashboardSession(
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=now + expires_in,
            user={
                "id": str(user["id"]),
                "username": user.get("username", "Discord user"),
                "global_name": user.get("global_name"),
                "avatar": user.get("avatar"),
            },
            expires_at=now + SESSION_SECONDS,
        )
        self.persist_sessions()
        result = web.HTTPFound(f"{self.config.frontend_url}/#dashboard")
        result.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
        result.del_cookie(OAUTH_STATE_COOKIE, path="/api/auth/callback", secure=True, httponly=True, samesite="Lax")
        return result

    def session(self, request: web.Request) -> DashboardSession | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        current = self._sessions.get(session_id)
        if current is None or current.expires_at <= time.time():
            if current is not None:
                self._sessions.pop(session_id, None)
                self.persist_sessions()
            return None
        return current

    async def refresh_access_token(self, session: DashboardSession) -> web.Response | None:
        if session.access_token_expires_at > time.time() + 60:
            return None
        async with self.require_http().post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
            },
        ) as response:
            if response.status in {400, 401}:
                return self.json_error("Discord sign-in expired. Please sign in again.", 401)
            if response.status != 200:
                print(f"Discord OAuth token refresh failed with status {response.status}.")
                return self.json_error("Could not refresh the Discord sign-in.", 502)
            token_payload = await response.json()
        access_token = token_payload.get("access_token")
        refresh_token = token_payload.get("refresh_token")
        expires_in = token_payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            return self.json_error("Discord returned an invalid refreshed token.", 502)
        session.access_token = access_token
        if isinstance(refresh_token, str) and refresh_token:
            session.refresh_token = refresh_token
        session.access_token_expires_at = time.time() + expires_in
        self.persist_sessions()
        return None

    def require_session(self, request: web.Request) -> DashboardSession | web.Response:
        session = self.session(request)
        return session if session is not None else self.json_error("Sign in with Discord to continue.", 401)

    async def discord_guilds(self, session: DashboardSession) -> list[dict[str, Any]] | web.Response:
        now = time.time()
        if session.guild_access is not None and session.guild_access_expires_at > now:
            return copy.deepcopy(session.guild_access)
        refreshed = await self.refresh_access_token(session)
        if refreshed is not None:
            return refreshed
        async with self.require_http().get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {session.access_token}"},
        ) as response:
            if response.status == 401:
                return self.json_error("Discord sign-in expired. Please sign in again.", 401)
            if response.status != 200:
                return self.json_error("Could not load your Discord servers.", 502)
            result = await response.json()
        if not isinstance(result, list):
            return self.json_error("Discord returned an invalid server list.", 502)
        session.guild_access = copy.deepcopy(result)
        session.guild_access_expires_at = now + GUILD_ACCESS_CACHE_SECONDS
        return result

    async def me(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        if isinstance(session, web.Response):
            return session
        user = dict(session.user)
        user_id = str(user.get("id", ""))
        avatar_hash = user.get("avatar")
        user["avatar_url"] = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
            if user_id and isinstance(avatar_hash, str) and avatar_hash
            else (
                f"https://cdn.discordapp.com/embed/avatars/{(int(user_id) >> 22) % 6}.png"
                if user_id.isdigit()
                else None
            )
        )
        return web.json_response({"user": user})

    async def logout(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            self._sessions.pop(session_id, None)
            self.persist_sessions()
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
        return response

    async def guilds(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        if isinstance(session, web.Response):
            return session
        user_guilds = await self.discord_guilds(session)
        if isinstance(user_guilds, web.Response):
            return user_guilds
        allowed = []
        for item in user_guilds:
            try:
                guild_id = int(item["id"])
                permissions = int(item.get("permissions", "0"))
            except (KeyError, TypeError, ValueError):
                continue
            can_manage = bool(item.get("owner")) or bool(permissions & (ADMINISTRATOR | MANAGE_GUILD))
            guild = self.bot.get_guild(guild_id)
            if can_manage and guild is not None:
                allowed.append({"id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else None})
        return web.json_response({"guilds": allowed})

    async def authorized_guild(self, request: web.Request) -> tuple[DashboardSession, discord.Guild] | web.Response:
        session = self.require_session(request)
        if isinstance(session, web.Response):
            return session
        try:
            guild_id = int(request.match_info["guild_id"])
        except (KeyError, TypeError, ValueError):
            return self.json_error("Invalid server ID.", 400)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return self.json_error("KRYO is not in this server.", 404)
        user_guilds = await self.discord_guilds(session)
        if isinstance(user_guilds, web.Response):
            return user_guilds
        for item in user_guilds:
            if str(item.get("id")) != str(guild_id):
                continue
            try:
                permissions = int(item.get("permissions", "0"))
            except (TypeError, ValueError):
                permissions = 0
            if item.get("owner") or permissions & (ADMINISTRATOR | MANAGE_GUILD):
                return session, guild
            return self.json_error("Only server owners or members with Manage Server can edit settings.", 403)
        return self.json_error("You do not have access to this server.", 403)

    @staticmethod
    def guild_settings(state: dict[str, Any], guild_id: int) -> dict[str, Any]:
        settings = state.get("settings", {}).get(str(guild_id), {})
        return settings if isinstance(settings, dict) else {}
    def settings_payload(self, guild: discord.Guild, user_id: int | None = None) -> dict[str, Any]:
        settings = self.guild_settings(self.state, guild.id)
        jail_cfg = settings.get("jail", {})
        jail_cfg = jail_cfg if isinstance(jail_cfg, dict) else {}
        try:
            configured_jail_role_id = int(jail_cfg.get("role_id", 0) or 0)
        except (TypeError, ValueError):
            configured_jail_role_id = 0
        security = settings.get("security", {})
        security = security if isinstance(security, dict) else {}
        modules_cfg = security.get("modules", {})
        modules_cfg = modules_cfg if isinstance(modules_cfg, dict) else {}
        punishments_cfg = security.get("punishments", {})
        punishments_cfg = punishments_cfg if isinstance(punishments_cfg, dict) else {}
        timeouts_cfg = security.get("timeouts", {})
        timeouts_cfg = timeouts_cfg if isinstance(timeouts_cfg, dict) else {}

        role_protection_cfg = settings.get("role_protection", {})
        role_protection_cfg = role_protection_cfg if isinstance(role_protection_cfg, dict) else {}
        role_protection = dict(ROLE_PROTECTION_DEFAULTS)
        for key, value in role_protection_cfg.items():
            if key in role_protection:
                role_protection[key] = value
        dangerous_permissions = role_protection.get("dangerous_permissions", list(ROLE_PROTECTION_DEFAULTS["dangerous_permissions"]))
        if not isinstance(dangerous_permissions, (list, tuple)):
            dangerous_permissions = list(ROLE_PROTECTION_DEFAULTS["dangerous_permissions"])
        dangerous_permissions = [
            permission for permission in dangerous_permissions
            if isinstance(permission, str) and permission.lower() in DANGEROUS_PERMISSIONS
        ]
        if not dangerous_permissions:
            dangerous_permissions = list(ROLE_PROTECTION_DEFAULTS["dangerous_permissions"])
        self_assignable_roles = role_protection.get("self_assignable_roles", [])
        if not isinstance(self_assignable_roles, (list, tuple)):
            self_assignable_roles = []
        parsed_roles: list[int] = []
        for raw_id in self_assignable_roles:
            if not isinstance(raw_id, (int, str)):
                continue
            try:
                parsed_roles.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        modules = {
            key: {
                "label": value.label,
                "description": value.description,
                "category": value.category,
                "enabled": bool(modules_cfg.get(key, False)),
                "punishable": key in PUNISHABLE_MODULES,
                "punishment": punishments_cfg.get(key, value.punishment),
                "timeout_minutes": self.timeout_minutes(key, timeouts_cfg.get(key)),
            }
            for key, value in PROTECTIONS.items()
        }
        welcome = settings.get("welcome", {})
        welcome = welcome if isinstance(welcome, dict) else {}

        welcome_color = welcome.get("color", "#2ECC71")
        if not isinstance(welcome_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", welcome_color):
            welcome_color = "#2ECC71"
        welcome_style = welcome.get("style", "embed")
        if not isinstance(welcome_style, str) or welcome_style not in {"embed", "plain", "image_card"}:
            welcome_style = "embed"
        welcome_font_size = welcome.get("font_size", 36)
        if type(welcome_font_size) is not int or not 18 <= welcome_font_size <= 56:
            welcome_font_size = 36
        welcome_title_font_size = welcome.get("title_font_size", 48)
        if type(welcome_title_font_size) is not int or not 24 <= welcome_title_font_size <= 72:
            welcome_title_font_size = 48
        autorole = settings.get("autorole", {})
        autorole = autorole if isinstance(autorole, dict) else {}
        self_roles = settings.get("selfroles", [])
        self_roles = self_roles if isinstance(self_roles, list) else []
        selfrole_panel = settings.get("selfrole_panel", {})
        selfrole_panel = selfrole_panel if isinstance(selfrole_panel, dict) else {}
        selfrole_options = selfrole_panel.get("options", [])
        if not isinstance(selfrole_options, list):
            selfrole_options = []
        labels_by_selfrole: dict[int, str] = {}
        for option in selfrole_options:
            if not isinstance(option, dict):
                continue
            role_id = option.get("role_id")
            label = option.get("label")
            if (
                isinstance(role_id, bool)
                or not isinstance(role_id, int)
                or not isinstance(label, str)
                or not label.strip()
            ):
                continue
            labels_by_selfrole[role_id] = label
        serialized_selfrole_options = []
        for role_id in self_roles:
            if isinstance(role_id, bool) or not isinstance(role_id, int):
                continue
            role = guild.get_role(role_id)
            if role is not None:
                serialized_selfrole_options.append({
                    "role_id": str(role_id),
                    "label": labels_by_selfrole.get(role_id, role.name),
                })
        logs = settings.get("log_channels", {})
        logs = logs if isinstance(logs, dict) else {}
        channels = [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ]
        voice_channels = [
            {"id": str(channel.id), "name": channel.name, "members": len(channel.members)}
            for channel in getattr(guild, "voice_channels", ())
        ]
        member_count = getattr(guild, "member_count", None)
        if not isinstance(member_count, int):
            member_count = len(getattr(guild, "members", ()))
        overview = {
            "members": member_count,
            "channels": len(getattr(guild, "channels", ())),
            "roles": len(getattr(guild, "roles", ())),
            "voice_members": sum(channel["members"] for channel in voice_channels),
        }
        voice_client = getattr(guild, "voice_client", None)
        connected_channel = getattr(voice_client, "channel", None)
        music_cog_getter = getattr(self.bot, "get_cog", None)
        music_cog = music_cog_getter("MusicPanel") if callable(music_cog_getter) else None
        music_state = None
        if music_cog is not None:
            states = getattr(music_cog, "states", {})
            if isinstance(states, dict):
                music_state = states.get(guild.id)
        roles = [
            {"id": str(role.id), "name": role.name, "dangerous": dangerous(role), "managed": role.managed, "jail_role": role.id == configured_jail_role_id}
            for role in guild.roles
            if not role.is_default() and guild.me is not None
            and role < guild.me.top_role
        ]
        emoji_guilds = [guild]
        emoji_guilds.extend(
            other_guild for other_guild in getattr(self.bot, "guilds", ())
            if other_guild.id != guild.id
        )
        emojis_by_id = {
            str(emoji.id): emoji
            for emoji_guild in emoji_guilds
            for emoji in getattr(emoji_guild, "emojis", ())
        }
        bot_user = getattr(self.bot, "user", None)
        invite_cog_getter = getattr(self.bot, "get_cog", None)
        invite_cog = invite_cog_getter("InviteTrackerCog") if callable(invite_cog_getter) else None
        invite_tracker = (
            invite_cog.dashboard_payload(guild, user_id)
            if invite_cog is not None
            else self.default_invite_tracker_payload(guild, settings)
        )
        ticketing = settings.get("tickets", {})
        ticketing = ticketing if isinstance(ticketing, dict) else {}
        ticket_cog_getter = getattr(self.bot, "get_cog", None)
        ticket_cog = ticket_cog_getter("TicketCog") if callable(ticket_cog_getter) else None
        live_tickets = (
            ticket_cog.dashboard_payload(guild)
            if ticket_cog is not None
            else {"active_ticket_count": 0, "active_tickets": []}
        )
        ticket_categories = ticketing.get("categories", DEFAULT_CATEGORIES)
        if not isinstance(ticket_categories, list) or not ticket_categories:
            ticket_categories = DEFAULT_CATEGORIES
        panel_color = ticketing.get("panel_color", "#5865F2")
        if not isinstance(panel_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", panel_color):
            panel_color = "#5865F2"
        jail_role = None
        role_getter = getattr(guild, "get_role", None)
        jail_role = role_getter(configured_jail_role_id) if configured_jail_role_id and callable(role_getter) else None
        jailed_members = jail_cfg.get("members", {})
        jailed_members = jailed_members if isinstance(jailed_members, dict) else {}
        jail_members = []
        member_getter = getattr(guild, "get_member", None)
        for member_id, record in jailed_members.items():
            if not isinstance(record, dict):
                continue
            try:
                member = member_getter(int(member_id)) if callable(member_getter) else None
            except (TypeError, ValueError):
                member = None
            jail_members.append({
                "id": str(member_id),
                "name": member.display_name if member is not None else str(record.get("name", "Unknown member")),
                "jailed_at": str(record.get("jailed_at", "")),
                "reason": str(record.get("reason", "")),
            })
        jail_members.sort(key=lambda item: item["name"].casefold())
        return {
            "guild": {
                "id": str(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
            },
            "voice_channels": voice_channels,
            "music": {
                "connected": connected_channel is not None,
                "channel_id": str(connected_channel.id) if connected_channel is not None else "",
                "channel_name": connected_channel.name if connected_channel is not None else "",
                "playing": bool(voice_client and getattr(voice_client, "current", None)),
                "stay_247": bool(getattr(music_state, "stay_247", False)),
                "stay_channel_id": str(getattr(music_state, "stay_channel_id", 0) or ""),
                "autoplay": bool(getattr(music_state, "autoplay", False)),
                "post_now_playing": bool(getattr(music_state, "post_now_playing", True)),
                "panel_channel_id": str(getattr(music_state, "text_channel_id", 0) or ""),
                "access_level": str(getattr(music_state, "access_level", "everyone")),
                "dj_role_id": str(getattr(music_state, "dj_role_id", 0) or ""),
                "loop": str(getattr(music_state, "loop", "off")),
                "filter": str(getattr(music_state, "filter", "normal")),
                "volume": int(getattr(voice_client, "volume", 100)) if voice_client is not None else 100,
                "paused": bool(getattr(voice_client, "paused", False)),
                "current": (
                    {
                        "title": voice_client.current.title,
                        "author": voice_client.current.author or "Unknown",
                        "length": int(getattr(voice_client.current, "length", 0) or 0),
                        "position": int(getattr(voice_client, "position", 0) or 0),
                    }
                    if voice_client is not None and getattr(voice_client, "current", None) is not None
                    else None
                ),
                "queue": [
                    {
                        "title": track.title,
                        "author": track.author or "Unknown",
                        "length": int(getattr(track, "length", 0) or 0),
                    }
                    for track in list(getattr(voice_client, "queue", ()))[:50]
                ] if voice_client is not None else [],
            },
            "overview": overview,
            "bot": {
                "name": bot_user.name if bot_user else "KRYO",
                "avatar": bot_user.display_avatar.url if bot_user else None,
            },
            "prime": bool(self.has_tier(guild.id, "prime")),
            "timeout_settings_enabled": True,
            "whitelist_user_names_enabled": True,
            "channels": channels,
            "category_channels": [
                {"id": str(channel.id), "name": channel.name}
                for channel in getattr(guild, "categories", ())
            ],
            "roles": roles,
            "modules": modules,
            "welcome": {
                "enabled": bool(welcome.get("enabled", False)),
                "channel_id": str(welcome.get("channel_id", 0) or ""),
                "leave_channel_id": str(welcome.get("leave_channel_id", 0) or ""),
                "title": str(welcome.get("title", "Welcome")),
                "message": str(welcome.get("message", "Welcome to **{server}**, {user}! You are member **#{count}**.")),
                "color": welcome_color.upper(),
                "image_url": str(welcome.get("image_url", "")),
                "video_url": str(welcome.get("video_url", "")),
                "style": welcome_style,
                "font_size": welcome_font_size,
                "title_font_size": welcome_title_font_size,
                "dm_enabled": bool(welcome.get("dm_enabled", False)),
                "leave_enabled": bool(welcome.get("leave_enabled", False)),
                "leave_message": str(welcome.get("leave_message", "**{name}** has left **{server}**.")),
            },

            "invite_tracker": invite_tracker,
            "ticketing": {
                "enabled": bool(ticketing.get("enabled", False)),
                "manage_channels_available": bool(
                    guild.me is not None and guild.me.guild_permissions.manage_channels
                ),
                "panel_channel_id": str(ticketing.get("panel_channel_id", 0) or ""),
                "panel_message_id": str(ticketing.get("panel_message_id", 0) or ""),
                "ticket_category_id": str(ticketing.get("ticket_category_id", 0) or ""),
                "staff_role_ids": [
                    str(value) for value in ticketing.get("staff_role_ids", [])
                    if isinstance(value, int)
                ] if isinstance(ticketing.get("staff_role_ids", []), list) else [],
                "transcript_channel_id": str(ticketing.get("transcript_channel_id", 0) or ""),
                "panel_title": str(ticketing.get("panel_title", "How can we help?")),
                "panel_description": str(ticketing.get("panel_description", "Choose a category below and our team will be with you shortly.")),
                "panel_tagline": str(ticketing.get("panel_tagline", "PRIVATE · ORGANIZED · PERSONAL")),
                "panel_tagline_description": str(ticketing.get("panel_tagline_description", "Your request is visible only to you and the support team. Select a category to get started.")),
                "panel_next_title": str(ticketing.get("panel_next_title", "WHAT HAPPENS NEXT")),
                "panel_next_description": str(ticketing.get("panel_next_description", "KRYO opens a private channel. Staff can claim, assign, and reply to your ticket.")),
                "panel_footer": str(ticketing.get("panel_footer", "KRYO · Select a category to get started")),
                "panel_color": panel_color.upper(),
                "panel_image_url": ticketing.get("panel_image_url", "")
                if isinstance(ticketing.get("panel_image_url", ""), str)
                else "",
                "open_limit": ticketing.get("open_limit", 1)
                if type(ticketing.get("open_limit", 1)) is int and 1 <= ticketing.get("open_limit", 1) <= 5
                else 1,
                "categories": [
                    {
                        "key": item.get("key", ""),
                        "label": item.get("label", ""),
                        "description": item.get("description", ""),
                        "emoji": item.get("emoji", ""),
                        "questions": item.get("questions", []),
                    }
                    for item in ticket_categories
                    if isinstance(item, dict)
                ],
                **live_tickets,
            },
            "jail": {
                "role_id": str(jail_role.id) if jail_role is not None else "",
                "role_name": jail_role.name if jail_role is not None else "",
                "members": jail_members,
                "manage_roles_available": bool(
                    guild.me is not None and guild.me.guild_permissions.manage_roles
                ),
            },
            "emojis": [
                {"name": emoji.name, "id": emoji_id, "animated": emoji.animated}
                for emoji_id, emoji in emojis_by_id.items()
            ],
            "autorole": {
                "human": [str(value) for value in autorole.get("human", []) if isinstance(value, int)],
                "bot": [str(value) for value in autorole.get("bot", []) if isinstance(value, int)],
            },
            "self_roles": {
                "role_ids": [str(value) for value in self_roles if isinstance(value, int) and not isinstance(value, bool)],
                "options": serialized_selfrole_options,
                "channel_id": str(selfrole_panel.get("channel_id", 0) or ""),
                "title": str(selfrole_panel.get("title", "Choose your roles")),
                "description": str(selfrole_panel.get(
                    "description",
                    "Choose any roles that fit you. Select a role again to remove it.",
                )),
            },
            "logs": {
                "channel_id": str(settings.get("log_channel_id", 0) or ""),
                "channels": {key: str(value) for key, value in logs.items() if key in LOG_TYPES},
                "types": [{"key": key, "label": value[1]} for key, value in LOG_TYPES.items()],
            },
            "whitelist": {
                "users": [str(value) for value in settings.get("antinuke_whitelist", []) if isinstance(value, int)],
                "roles": [str(value) for value in settings.get("antinuke_whitelist_roles", []) if isinstance(value, int)],
                "role_names": {
                    str(value): role.name
                    for value in settings.get("antinuke_whitelist_roles", [])
                    if isinstance(value, int)
                    for role in getattr(guild, "roles", ())
                    if role.id == value
                },
            },
        }

    @staticmethod
    def timeout_minutes(module: str, configured: Any) -> int:
        default = (
            DEFAULT_SPAM_TIMEOUT_MINUTES
            if module == "antispam"
            else DEFAULT_TIMEOUT_MINUTES
        )
        if type(configured) is int and MIN_TIMEOUT_MINUTES <= configured <= MAX_TIMEOUT_MINUTES:
            return configured
        return default

    async def whitelist_user_names(self, guild: discord.Guild, user_ids: list[str]) -> dict[str, str]:
        semaphore = asyncio.Semaphore(5)

        async def resolve_name(user_id: str) -> tuple[str, str]:
            async with semaphore:
                discord_id = int(user_id)
                member = guild.get_member(discord_id)
                if member is not None:
                    return user_id, member.name
                try:
                    member = await guild.fetch_member(discord_id)
                    return user_id, member.name
                except discord.NotFound:
                    pass
                except discord.HTTPException as error:
                    print(f"Dashboard could not fetch member {discord_id} in guild {guild.id}: {type(error).__name__}: {error}")
                try:
                    user = await self.bot.fetch_user(discord_id)
                    return user_id, user.name
                except discord.NotFound:
                    return user_id, "Unknown member"
                except discord.HTTPException as error:
                    print(f"Dashboard could not fetch user {discord_id}: {type(error).__name__}: {error}")
                    return user_id, "Unknown member"

        return dict(await asyncio.gather(*(resolve_name(user_id) for user_id in user_ids)))

    async def get_settings(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        _, guild = authorized
        session, guild = authorized
        try:
            user_id = int(getattr(session, "user", {})["id"])
        except (KeyError, TypeError, ValueError):
            user_id = None
        cog_getter = getattr(self.bot, "get_cog", None)
        ticket_cog = cog_getter("TicketCog") if callable(cog_getter) else None
        discover_panel = getattr(ticket_cog, "discover_existing_panel", None)
        if callable(discover_panel):
            try:
                await discover_panel(guild)
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Dashboard could not inspect the existing ticket panel in guild {guild.id}: {type(error).__name__}: {error}")
        payload = self.settings_payload(guild, user_id)
        if not payload["voice_channels"]:
            fetch_channels = getattr(guild, "fetch_channels", None)
            if callable(fetch_channels):
                try:
                    fetched_channels = await fetch_channels()
                except (discord.Forbidden, discord.HTTPException):
                    fetched_channels = ()
                payload["voice_channels"] = [
                    {
                        "id": str(channel.id),
                        "name": channel.name,
                        "members": len(getattr(channel, "members", ())),
                    }
                    for channel in fetched_channels
                    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
                ]
        payload["whitelist"]["user_names"] = await self.whitelist_user_names(guild, payload["whitelist"]["users"])
        return web.json_response(payload)

    async def update_settings(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        session, guild = authorized
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException):
            return self.json_error("Request body must be valid JSON.", 400)
        if not isinstance(payload, dict):
            return self.json_error("Request body must be an object.", 400)
        settings_by_guild = self.state.get("settings", {})
        current_settings = settings_by_guild.get(str(guild.id), {}) if isinstance(settings_by_guild, dict) else {}
        settings = copy.deepcopy(current_settings) if isinstance(current_settings, dict) else {}
        jail_cfg = settings.get("jail", {})
        jail_cfg = jail_cfg if isinstance(jail_cfg, dict) else {}
        try:
            configured_jail_role_id = int(jail_cfg.get("role_id", 0) or 0)
        except (TypeError, ValueError):
            configured_jail_role_id = 0
        security = settings.setdefault("security", {})
        modules_cfg = security.setdefault("modules", {})
        punishments_cfg = security.setdefault("punishments", {})
        changed = False

        modules = payload.get("modules")
        if modules is not None:
            if not isinstance(modules, dict) or any(key not in PROTECTIONS or type(value) is not bool for key, value in modules.items()):
                return self.json_error("Invalid protection settings.", 400)
            if any(value and not modules_cfg.get(key, False) for key, value in modules.items()) and not self.has_tier(guild.id, "prime"):
                return self.json_error("Enabling antinuke and chat protections requires Prime.", 403)
            for key, value in modules.items():
                modules_cfg[key] = value
            settings["antinuke_enabled"] = any(
                modules_cfg.get(key, False)
                for key in NUKE_MODULES
            )
            changed = True

        role_protection = payload.get("role_protection")
        if role_protection is not None:
            if not isinstance(role_protection, dict):
                return self.json_error("Invalid role protection settings.", 400)
            allowed_keys = set(ROLE_PROTECTION_DEFAULTS) | {"enabled"}
            if any(key not in allowed_keys for key in role_protection):
                return self.json_error("Invalid role protection settings.", 400)
            current_role_protection = settings.setdefault("role_protection", {})
            for key in ("enabled", "whitelist_strict", "ignore_onboarding_roles"):
                if key in role_protection and type(role_protection[key]) is not bool:
                    return self.json_error(f"{key} must be true or false.", 400)
            if "dangerous_permissions" in role_protection:
                dangerous_permissions = role_protection["dangerous_permissions"]
                if not isinstance(dangerous_permissions, list):
                    return self.json_error("dangerous_permissions must be a list of permission names.", 400)
                valid_permissions: list[str] = []
                seen: set[str] = set()
                for item in dangerous_permissions:
                    permission = str(item).strip().lower()
                    if permission not in DANGEROUS_PERMISSIONS or permission in seen:
                        return self.json_error("dangerous_permissions contains an invalid permission name.", 400)
                    valid_permissions.append(permission)
                    seen.add(permission)
                current_role_protection["dangerous_permissions"] = valid_permissions
            if "self_assignable_roles" in role_protection:
                self_assignable_roles = role_protection["self_assignable_roles"]
                if not isinstance(self_assignable_roles, list):
                    return self.json_error("self_assignable_roles must be a list of role IDs.", 400)
                valid_roles: list[int] = []
                seen_roles: set[int] = set()
                for raw_id in self_assignable_roles:
                    try:
                        role_id = int(raw_id)
                    except (TypeError, ValueError):
                        return self.json_error("self_assignable_roles must contain valid role IDs.", 400)
                    if role_id in seen_roles:
                        continue
                    valid_roles.append(role_id)
                    seen_roles.add(role_id)
                current_role_protection["self_assignable_roles"] = valid_roles
            for key, validator in (
                ("audit_retry_delay", lambda value: isinstance(value, (int, float)) and 0.1 <= float(value) <= 60.0),
                ("audit_max_age", lambda value: isinstance(value, (int, float)) and 0.0 <= float(value) <= 60.0),
                ("grant_punish_at", lambda value: isinstance(value, int) and 1 <= value <= 25),
                ("grant_window", lambda value: isinstance(value, (int, float)) and 10.0 <= float(value) <= 3600.0),
            ):
                if key in role_protection:
                    if not validator(role_protection[key]):
                        return self.json_error(f"{key} must be within the supported range.", 400)
                    current_role_protection[key] = type(ROLE_PROTECTION_DEFAULTS[key])(role_protection[key])
            if "enabled" in role_protection:
                current_role_protection["enabled"] = bool(role_protection["enabled"])
            for key in ("whitelist_strict", "ignore_onboarding_roles"):
                if key in role_protection:
                    current_role_protection[key] = bool(role_protection[key])
            changed = True

        punishments = payload.get("punishments")
        if punishments is not None:
            if not isinstance(punishments, dict) or any(
                key not in PROTECTIONS
                or key not in PUNISHABLE_MODULES
                or not isinstance(value, str)
                or value not in {"ban", "kick", "timeout"}
                for key, value in punishments.items()
            ):
                return self.json_error("Invalid protection punishment settings.", 400)
            punishments_cfg.update(punishments)
            changed = True

        timeouts = payload.get("timeouts")
        if timeouts is not None:
            if not isinstance(timeouts, dict) or any(
                key not in PROTECTIONS
                or key not in PUNISHABLE_MODULES
                or type(value) is not int
                or not MIN_TIMEOUT_MINUTES <= value <= MAX_TIMEOUT_MINUTES
                for key, value in timeouts.items()
            ):
                return self.json_error("Timeouts must be whole minutes between 1 and 40320 for punishable protections.", 400)
            effective_punishments = {
                key: punishments_cfg.get(key) or value.punishment
                for key, value in PROTECTIONS.items()
            }
            if any(effective_punishments[key] != "timeout" for key in timeouts):
                return self.json_error("Set a protection's punishment to Timeout before choosing its duration.", 400)
            settings["security"].setdefault("timeouts", {}).update(timeouts)
            changed = True
        welcome = payload.get("welcome")
        if welcome is not None:
            if not isinstance(welcome, dict) or any(
                key not in {
                    "enabled",
                    "channel_id",
                    "leave_channel_id",
                    "title",
                    "message",
                    "image_url",
                    "video_url",
                    "style",
                    "font_size",
                    "title_font_size",
                    "dm_enabled",
                    "leave_enabled",
                    "leave_message",
                    "color",
                }
                for key in welcome
            ):
                return self.json_error("Invalid welcome message settings.", 400)
            current = copy.deepcopy(settings.get("welcome", {}))
            for key in ("enabled", "leave_enabled", "dm_enabled"):
                if key in welcome:
                    if type(welcome[key]) is not bool:
                        return self.json_error(f"{key} must be true or false.", 400)
                    current[key] = welcome[key]
            for key, limit in (("message", 2000), ("leave_message", 1000)):
                if key in welcome:
                    if not isinstance(welcome[key], str) or len(welcome[key]) > limit:
                        return self.json_error(
                            f"{key} must be text with at most {limit} characters.", 400
                        )
                    current[key] = welcome[key]
            if "title" in welcome:
                title = welcome["title"]
                if not isinstance(title, str) or len(title) > 256:
                    return self.json_error("title must be text with at most 256 characters.", 400)
                current["title"] = title
            if "color" in welcome:
                color = welcome["color"]
                if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    return self.json_error("Welcome color must be a six-digit hexadecimal color.", 400)
                current["color"] = color.upper()
            if "image_url" in welcome:
                image_url = welcome["image_url"]
                if not isinstance(image_url, str) or len(image_url) > 2048:
                    return self.json_error("Welcome image URL must be at most 2048 characters.", 400)
                if image_url:
                    try:
                        parsed_image = urlsplit(image_url)
                        valid_image_url = (
                            parsed_image.scheme in {"http", "https"}
                            and bool(parsed_image.hostname)
                            and parsed_image.username is None
                            and parsed_image.password is None
                            and not any(char.isspace() for char in image_url)
                            and (parsed_image.port is None or 1 <= parsed_image.port <= 65535)
                        )
                    except ValueError:
                        valid_image_url = False
                    if not valid_image_url:
                        return self.json_error("Welcome image must be a valid HTTP or HTTPS URL.", 400)
                current["image_url"] = image_url
            if "video_url" in welcome:
                video_url = welcome["video_url"]
                if not isinstance(video_url, str) or len(video_url) > 1000:
                    return self.json_error("Welcome video URL must be at most 1000 characters.", 400)
                if video_url:
                    try:
                        parsed_video = urlsplit(video_url)
                        valid_video_url = (
                            parsed_video.scheme in {"http", "https"}
                            and bool(parsed_video.hostname)
                            and parsed_video.username is None
                            and parsed_video.password is None
                            and not any(char.isspace() for char in video_url)
                            and (parsed_video.port is None or 1 <= parsed_video.port <= 65535)
                        )
                    except ValueError:
                        valid_video_url = False
                    if not valid_video_url:
                        return self.json_error("Welcome video must be a valid HTTP or HTTPS URL.", 400)
                current["video_url"] = video_url
            if "style" in welcome:
                style = welcome["style"]
                if not isinstance(style, str) or style not in {"embed", "plain", "image_card"}:
                    return self.json_error("Choose Embed, Plain text, or Image card for the welcome style.", 400)
                current["style"] = style
            if "font_size" in welcome:
                font_size = welcome["font_size"]
                if type(font_size) is not int or not 18 <= font_size <= 56:
                    return self.json_error("Welcome card font size must be a whole number from 18 to 56.", 400)
                current["font_size"] = font_size
            if "title_font_size" in welcome:
                title_font_size = welcome["title_font_size"]
                if type(title_font_size) is not int or not 24 <= title_font_size <= 72:
                    return self.json_error("Welcome title font size must be a whole number from 24 to 72.", 400)
                current["title_font_size"] = title_font_size
            if "channel_id" in welcome:
                channel = self.text_channel(guild, welcome["channel_id"])
                if welcome["channel_id"] and channel is None:
                    return self.json_error("Select a text channel from this server.", 400)
                current["channel_id"] = channel.id if channel is not None else 0

            if "leave_channel_id" in welcome:
                channel = self.text_channel(guild, welcome["leave_channel_id"])
                if welcome["leave_channel_id"] and channel is None:
                    return self.json_error("Select a text channel from this server for leave messages.", 400)
                current["leave_channel_id"] = channel.id if channel is not None else 0
            if current.get("enabled") and not current.get("channel_id"):
                return self.json_error("Choose a welcome channel before enabling welcome messages.", 400)
            settings["welcome"] = current
            changed = True

        autorole = payload.get("autorole")
        if autorole is not None:
            if not isinstance(autorole, dict) or any(key not in {"human", "bot"} for key in autorole):
                return self.json_error("Invalid autorole settings.", 400)
            current = settings.setdefault("autorole", {"human": [], "bot": []})
            if guild.me is None:
                return self.json_error("The bot is not ready in this server.", 503)
            for key, values in autorole.items():
                if not isinstance(values, list) or len(values) > 50:
                    return self.json_error("Select at most 50 autoroles.", 400)
                selected: list[int] = []
                for raw_id in values:
                    role = self.role(guild, raw_id)

                    if (
                        role is None
                        or role >= guild.me.top_role
                        or role.managed
                        or role.id == configured_jail_role_id
                        or dangerous(role)
                    ):
                        return self.json_error(
                            "An autorole is missing, unsafe, or above the bot's role.",
                            400,
                        )
                    if role.id not in selected:
                        selected.append(role.id)
                current[key] = selected
            changed = True

        self_roles = payload.get("self_roles")
        if self_roles is not None:
            allowed_self_role_fields = {"role_ids", "options", "channel_id", "title", "description"}
            if not isinstance(self_roles, dict) or any(
                key not in allowed_self_role_fields for key in self_roles
            ):
                return self.json_error("Invalid self-role panel settings.", 400)
            raw_options = self_roles.get("options")
            if raw_options is not None and (
                not isinstance(raw_options, list) or len(raw_options) > 25
            ):
                return self.json_error("Create up to 25 self-role options.", 400)
            if raw_options is not None and any(
                not isinstance(option, dict) or set(option) != {"role_id", "label"}
                for option in raw_options
            ):
                return self.json_error("Each self-role option must include only its role ID and text.", 400)
            raw_role_ids = self_roles.get("role_ids", settings.get("selfroles", []))
            if raw_options is None and (not isinstance(raw_role_ids, list) or len(raw_role_ids) > 25):
                return self.json_error("Choose up to 25 self-assignable roles.", 400)
            if guild.me is None:
                return self.json_error("The bot is not ready in this server.", 503)
            selected_input = raw_options if raw_options is not None else raw_role_ids
            if selected_input and not guild.me.guild_permissions.manage_roles:
                return self.json_error("KRYO needs Manage Roles to assign self roles.", 403)
            selected_self_roles: list[int] = []
            panel_options: list[dict[str, Any]] = []
            raw_selections = (
                [
                    (option.get("role_id"), option.get("label"))
                    if isinstance(option, dict)
                    else (None, None)
                    for option in raw_options
                ]
                if raw_options is not None
                else [(raw_id, None) for raw_id in raw_role_ids]
            )
            for raw_id, option_label in raw_selections:
                if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not str(raw_id).isdigit():
                    return self.json_error("Self-role selections must contain valid role IDs.", 400)
                if raw_options is not None and (
                    not isinstance(option_label, str)
                    or not option_label.strip()
                    or len(option_label.strip()) > 100
                ):
                    return self.json_error("Each self-role option needs text between 1 and 100 characters.", 400)
                role = self.role(guild, raw_id)
                if (
                    role is None
                    or role >= guild.me.top_role
                    or role.managed
                    or role.id == configured_jail_role_id
                    or dangerous(role)
                ):
                    return self.json_error(
                        "A self-role is missing, unsafe, managed, or above the bot's role.",
                        400,
                    )
                if role.id in selected_self_roles:
                    return self.json_error("Each self-role option must use a different role.", 400)
                selected_self_roles.append(role.id)
                panel_options.append({
                    "role_id": role.id,
                    "label": option_label.strip() if isinstance(option_label, str) else role.name,
                })
            current_panel = copy.deepcopy(settings.get("selfrole_panel", {}))
            if not isinstance(current_panel, dict):
                current_panel = {}
            if "channel_id" in self_roles:
                channel_id = self_roles["channel_id"]
                channel = self.text_channel(guild, channel_id)
                if channel_id and channel is None:
                    return self.json_error("Choose a text channel from this server for the self-role panel.", 400)
                current_panel["channel_id"] = channel.id if channel is not None else 0
            if "title" in self_roles:
                title = self_roles["title"]
                if not isinstance(title, str) or not title.strip() or len(title) > 256:
                    return self.json_error("Self-role panel title must contain at most 256 characters.", 400)
                current_panel["title"] = title.strip()
            if "description" in self_roles:
                description = self_roles["description"]
                if not isinstance(description, str) or not description.strip() or len(description) > 4000:
                    return self.json_error("Self-role panel message must contain at most 4000 characters.", 400)
                current_panel["description"] = description.strip()
            if raw_options is not None or "role_ids" in self_roles:
                current_panel["options"] = panel_options
            settings["selfrole_panel"] = current_panel
            settings["selfroles"] = selected_self_roles
            changed = True

        logs = payload.get("logs")
        if logs is not None:
            if not isinstance(logs, dict) or any(key not in {"channel_id", "channels"} for key in logs):
                return self.json_error("Invalid log settings.", 400)
            if "channel_id" in logs:
                channel = self.text_channel(guild, logs["channel_id"])
                if logs["channel_id"] and channel is None:
                    return self.json_error("Select a log channel from this server.", 400)
                settings["log_channel_id"] = channel.id if channel is not None else 0
                changed = True
            if "channels" in logs:
                channels = logs["channels"]
                if not self.has_tier(guild.id, "prime"):
                    return self.json_error("Per-event log channels require Prime.", 403)
                if not isinstance(channels, dict) or any(key not in LOG_TYPES for key in channels):
                    return self.json_error("Invalid per-event log channels.", 400)
                selected_channels = {}
                for key, raw_id in channels.items():
                    channel = self.text_channel(guild, raw_id)
                    if raw_id and channel is None:
                        return self.json_error("Select log channels from this server.", 400)
                    if channel is not None:
                        selected_channels[key] = channel.id
                settings["log_channels"] = selected_channels
                if selected_channels.get("antinuke"):
                    settings["log_channel_id"] = selected_channels["antinuke"]
                changed = True

        whitelist = payload.get("whitelist")
        if whitelist is not None:
            if not isinstance(whitelist, dict) or any(key not in {"users", "roles"} for key in whitelist):
                return self.json_error("Invalid whitelist settings.", 400)
            for key, target_key in (("users", "antinuke_whitelist"), ("roles", "antinuke_whitelist_roles")):
                if key not in whitelist:
                    continue
                values = whitelist[key]
                if not isinstance(values, list) or len(values) > 100:
                    return self.json_error("A whitelist can contain at most 100 entries.", 400)
                selected: list[int] = []
                for raw_id in values:
                    if key == "roles":
                        role = self.role(guild, raw_id)
                        if role is None and isinstance(raw_id, str):
                            wanted = raw_id.lstrip("@").casefold()
                            role = next(
                                (candidate for candidate in guild.roles if candidate.name.casefold() == wanted),
                                None,
                            )
                        valid = role is not None
                        if valid:
                            raw_id = role.id
                    else:
                        try:
                            valid = guild.get_member(int(raw_id)) is not None
                        except (TypeError, ValueError):
                            valid = False
                        raw_identifier = str(raw_id).strip()
                        jail_cog_getter = getattr(self.bot, "get_cog", None)
                        jail_cog = jail_cog_getter("JailCog") if callable(jail_cog_getter) else None
                        if not isinstance(jail_cog, JailCog):
                            return self.json_error("Member lookup is unavailable. Try again later.", 503)
                        try:
                            member = await jail_cog.resolve_member_reference(guild, raw_identifier)
                        except JailActionError as error:
                            return self.json_error(str(error), error.status)
                        valid = member is not None
                        if valid:
                            raw_id = member.id
                    if not valid:
                        return self.json_error("Every whitelist entry must be a current server member or role.", 400)
                    selected.append(int(raw_id))
                settings[target_key] = list(dict.fromkeys(selected))
            changed = True

        invite_tracker = payload.get("invite_tracker")
        initialize_invite_cache = False
        if invite_tracker is not None:
            if not isinstance(invite_tracker, dict) or any(
                key not in {"enabled", "log_channel_id", "minimum_account_age_days"}
                for key in invite_tracker
            ):
                return self.json_error("Invalid invite tracker settings.", 400)
            current = settings.setdefault("invite_tracker", {})
            if "enabled" in invite_tracker:
                if type(invite_tracker["enabled"]) is not bool:
                    return self.json_error("Invite tracker enabled must be true or false.", 400)
                initialize_invite_cache = (
                    invite_tracker["enabled"] and not current.get("enabled", False)
                )
                if invite_tracker["enabled"]:
                    me = guild.me
                    if me is None or not me.guild_permissions.manage_guild:
                        return self.json_error(
                            "KRYO needs Manage Server permission to track invite usage.",
                            400,
                        )
                current["enabled"] = invite_tracker["enabled"]
            if "log_channel_id" in invite_tracker:
                channel = self.text_channel(guild, invite_tracker["log_channel_id"])
                if invite_tracker["log_channel_id"] and channel is None:
                    return self.json_error("Select a text channel from this server for invite logs.", 400)
                current["log_channel_id"] = channel.id if channel is not None else 0
            if "minimum_account_age_days" in invite_tracker:
                age_days = invite_tracker["minimum_account_age_days"]
                if type(age_days) is not int or not 0 <= age_days <= 90:
                    return self.json_error("Minimum account age must be a whole number between 0 and 90 days.", 400)
                current["minimum_account_age_days"] = age_days
            changed = True

        ticketing = payload.get("ticketing")
        if ticketing is not None:
            allowed_keys = {
                "enabled", "panel_channel_id", "ticket_category_id", "staff_role_ids",
                "transcript_channel_id", "panel_title", "panel_description",
                "panel_tagline", "panel_tagline_description", "panel_next_title",
                "panel_next_description", "panel_footer",
                "panel_color", "panel_image_url", "open_limit", "categories",
            }
            if not isinstance(ticketing, dict) or any(key not in allowed_keys for key in ticketing):
                return self.json_error("Invalid ticket support settings.", 400)
            current = settings.setdefault("tickets", {})
            if not isinstance(current, dict):
                current = {}
                settings["tickets"] = current
            if "enabled" in ticketing:
                if type(ticketing["enabled"]) is not bool:
                    return self.json_error("Ticket support enabled must be true or false.", 400)
                if ticketing["enabled"] and (
                    guild.me is None or not guild.me.guild_permissions.manage_channels
                ):
                    return self.json_error("KRYO needs Manage Channels permission to create private tickets.", 400)
                current["enabled"] = ticketing["enabled"]
            if "panel_channel_id" in ticketing:
                channel = self.text_channel(guild, ticketing["panel_channel_id"])
                if ticketing["panel_channel_id"] and channel is None:
                    return self.json_error("Select a text channel from this server for the ticket panel.", 400)
                current["panel_channel_id"] = channel.id if channel is not None else 0
            if "ticket_category_id" in ticketing:
                raw_id = ticketing["ticket_category_id"]
                try:
                    category = guild.get_channel(int(raw_id)) if raw_id else None
                except (TypeError, ValueError):
                    category = None
                if raw_id and not isinstance(category, discord.CategoryChannel):
                    return self.json_error("Select a ticket category from this server.", 400)
                current["ticket_category_id"] = category.id if category is not None else 0
            if "transcript_channel_id" in ticketing:
                channel = self.text_channel(guild, ticketing["transcript_channel_id"])
                if ticketing["transcript_channel_id"] and channel is None:
                    return self.json_error("Select a text channel from this server for ticket transcripts.", 400)
                current["transcript_channel_id"] = channel.id if channel is not None else 0
            if "staff_role_ids" in ticketing:
                role_ids = ticketing["staff_role_ids"]
                if not isinstance(role_ids, list) or len(role_ids) > 20:
                    return self.json_error("Select at most 20 ticket staff roles.", 400)
                selected_roles = []
                for raw_id in role_ids:
                    role = self.role(guild, raw_id)
                    if role is None or guild.me is None or role >= guild.me.top_role:
                        return self.json_error("Select valid ticket staff roles below KRYO's highest role.", 400)
                    if role.id not in selected_roles:
                        selected_roles.append(role.id)
                current["staff_role_ids"] = selected_roles
            for key, maximum in (
                ("panel_title", 128),
                ("panel_description", 1000),
                ("panel_tagline", 220),
                ("panel_tagline_description", 1024),
                ("panel_next_title", 256),
                ("panel_next_description", 1024),
                ("panel_footer", 1024),
            ):
                if key in ticketing:
                    value = ticketing[key]
                    if not isinstance(value, str) or len(value) > maximum:
                        return self.json_error(f"{key} must be text with at most {maximum} characters.", 400)
                    current[key] = value
            if "panel_color" in ticketing:
                color = ticketing["panel_color"]
                if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    return self.json_error("Ticket panel color must be a six-digit hexadecimal color.", 400)
                current["panel_color"] = color.upper()
            if "panel_image_url" in ticketing:
                image_url = ticketing["panel_image_url"]
                if not isinstance(image_url, str) or len(image_url) > 2048:
                    return self.json_error("Ticket panel image URL must be a valid HTTPS URL under 2048 characters.", 400)
                image_url = image_url.strip()
                if image_url:
                    try:
                        parsed_image_url = urlsplit(image_url)
                        valid_image_url = (
                            parsed_image_url.scheme == "https"
                            and bool(parsed_image_url.hostname)
                            and parsed_image_url.username is None
                            and parsed_image_url.password is None
                        )
                    except ValueError:
                        valid_image_url = False
                    if not valid_image_url:
                        return self.json_error("Ticket panel image URL must be a valid HTTPS URL.", 400)
                current["panel_image_url"] = image_url
            if "open_limit" in ticketing:
                open_limit = ticketing["open_limit"]
                if type(open_limit) is not int or not 1 <= open_limit <= 5:
                    return self.json_error("The open ticket limit must be a whole number between 1 and 5.", 400)
                current["open_limit"] = open_limit
            if "categories" in ticketing:
                categories = ticketing["categories"]
                if not isinstance(categories, list) or not 1 <= len(categories) <= 25:
                    return self.json_error("Add between 1 and 25 ticket categories.", 400)
                normalized_categories = []
                seen_keys = set()
                for item in categories:
                    if not isinstance(item, dict) or any(
                        key not in {"key", "label", "description", "emoji", "questions"}
                        for key in item
                    ):
                        return self.json_error("Each ticket category must have valid category fields.", 400)
                    key = item.get("key")
                    label = item.get("label")
                    description = item.get("description", "")
                    emoji = item.get("emoji", "")
                    questions = item.get("questions", [])
                    if (
                        not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", key)
                        or key in seen_keys
                        or not isinstance(label, str) or not label.strip() or len(label.strip()) > 80
                        or not isinstance(description, str) or len(description) > 100
                        or not isinstance(emoji, str) or len(emoji) > 64
                        or not isinstance(questions, list) or len(questions) > 5
                        or any(not isinstance(question, str) or not question.strip() or len(question.strip()) > 45 for question in questions)
                    ):
                        return self.json_error("Ticket categories need unique keys, a label, and up to five questions of at most 45 characters.", 400)
                    seen_keys.add(key)
                    normalized_categories.append({
                        "key": key,
                        "label": label.strip(),
                        "description": description.strip(),
                        "emoji": emoji.strip(),
                        "questions": [question.strip() for question in questions],
                    })
                current["categories"] = normalized_categories
            if current.get("enabled"):
                if not current.get("panel_channel_id"):
                    return self.json_error("Choose a panel channel before enabling ticket support.", 400)
                if not current.get("transcript_channel_id"):
                    return self.json_error("Choose a staff transcript log channel before enabling ticket support.", 400)
                if not current.get("categories"):
                    return self.json_error("Add at least one category before enabling ticket support.", 400)
                transcript_channel = self.text_channel(guild, current["transcript_channel_id"])
                me = guild.me
                permissions = transcript_channel.permissions_for(me) if transcript_channel is not None and me is not None else None
                if permissions is None or not permissions.send_messages or not permissions.embed_links or not permissions.attach_files:
                    return self.json_error("KRYO needs Send Messages, Embed Links, and Attach Files in the transcript channel.", 403)
            changed = True

        if changed:
            self.state.setdefault("settings", {})[str(guild.id)] = settings
            if whitelist is not None:
                security_cog = self.bot.get_cog("SecurityCog")
                if isinstance(security_cog, SecurityCog):
                    security_cog.invalidate_whitelist_cache(guild.id)
            self.save()

        if initialize_invite_cache:
            get_cog = getattr(self.bot, "get_cog", None)
            invite_cog = get_cog("InviteTrackerCog") if callable(get_cog) else None
            if invite_cog is not None:
                asyncio.create_task(invite_cog.refresh_guild(guild))
        try:
            user_id = int(getattr(session, "user", {})["id"])
        except (KeyError, TypeError, ValueError):
            user_id = None
        response_payload = self.settings_payload(guild, user_id)
        if whitelist is not None:
            response_payload["whitelist"]["user_names"] = await self.whitelist_user_names(
                guild,
                response_payload["whitelist"]["users"],
            )
        return web.json_response(response_payload)

    async def send_announcement(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        _, guild = authorized
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException):
            return self.json_error("Request body must be valid JSON.", 400)
        if not isinstance(payload, dict) or any(
            key not in {"channel_id", "title", "message", "color", "image_url", "allow_everyone"}
            for key in payload
        ):
            return self.json_error("Invalid announcement.", 400)
        channel = self.text_channel(guild, payload.get("channel_id"))
        if channel is None:
            return self.json_error("Choose a text channel from this server.", 400)
        title = payload.get("title", "")
        message = payload.get("message", "")
        color = payload.get("color", "#F39C12")
        image_url = payload.get("image_url", "")
        allow_everyone = payload.get("allow_everyone", False)
        if not isinstance(title, str) or len(title) > 256:
            return self.json_error("Announcement title must be text with at most 256 characters.", 400)
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            return self.json_error("Announcement message must contain text with at most 4000 characters.", 400)
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return self.json_error("Announcement color must be a six-digit hexadecimal color.", 400)
        if not isinstance(image_url, str) or len(image_url) > 2048:
            return self.json_error("Announcement image URL must be at most 2048 characters.", 400)
        if image_url:
            try:
                parsed_image = urlsplit(image_url)
                valid_image_url = (
                    parsed_image.scheme in {"http", "https"}
                    and bool(parsed_image.hostname)
                    and parsed_image.username is None
                    and parsed_image.password is None
                    and not any(char.isspace() for char in image_url)
                    and (parsed_image.port is None or 1 <= parsed_image.port <= 65535)
                )
            except ValueError:
                valid_image_url = False
            if not valid_image_url:
                return self.json_error("Announcement image must be a valid HTTP or HTTPS URL.", 400)
        if type(allow_everyone) is not bool:
            return self.json_error("allow_everyone must be true or false.", 400)
        me = guild.me
        if me is None:
            return self.json_error("KRYO is not ready in this server.", 503)
        permissions = channel.permissions_for(me)
        if not permissions.send_messages or not permissions.embed_links:
            return self.json_error("KRYO needs Send Messages and Embed Links in the selected channel.", 403)
        ping_tokens = list(dict.fromkeys(
            re.findall(r"@(everyone|here)\b", message, re.IGNORECASE)
        )) if allow_everyone else []
        if ping_tokens and not permissions.mention_everyone:
            return self.json_error("KRYO needs Mention Everyone permission to send this ping.", 403)
        embed_message = re.sub(
            r"@(everyone|here)\b",
            "",
            message,
            flags=re.IGNORECASE,
        ).strip() if ping_tokens else message
        embed = ui.announcement(
            guild,
            title,
            embed_message or None,
            color,
            image_url,
        )
        try:
            await channel.send(
                content=" ".join(f"@{token.lower()}" for token in ping_tokens) or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=False,
                    roles=False,
                    everyone=allow_everyone,
                    replied_user=False,
                ),
            )
        except discord.Forbidden:
            return self.json_error("KRYO cannot send announcements in that channel.", 403)
        except discord.HTTPException as error:
            print(f"Announcement send failed in guild {guild.id}, channel {channel.id}: {type(error).__name__}: {error}")
            return self.json_error("Discord could not send this announcement.", 502)
        return web.json_response({"ok": True, "channel_id": str(channel.id)})

    async def publish_ticket_panel(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        _, guild = authorized
        cog_getter = getattr(self.bot, "get_cog", None)
        cog = cog_getter("TicketCog") if callable(cog_getter) else None
        if cog is None:
            return self.json_error("Ticket support is not available in this bot instance.", 503)
        cfg = self.guild_settings(self.state, guild.id).get("tickets", {})
        channel = self.text_channel(guild, cfg.get("panel_channel_id", 0)) if isinstance(cfg, dict) else None
        if channel is None:
            return self.json_error("Choose a valid ticket panel channel and save its settings first.", 400)
        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            return self.json_error("KRYO needs Manage Channels permission to create private tickets.", 403)
        permissions = channel.permissions_for(me)
        if not permissions.send_messages or not permissions.embed_links:
            return self.json_error("KRYO needs Send Messages and Embed Links in the panel channel.", 403)
        try:
            message = await cog.publish_panel(guild)
        except ValueError as error:
            return self.json_error(str(error), 400)
        except discord.Forbidden:
            return self.json_error("KRYO cannot publish a ticket panel in that channel.", 403)
        except discord.HTTPException as error:
            print(f"Ticket panel publish failed in guild {guild.id}: {type(error).__name__}: {error}")
            return self.json_error("Discord could not publish the ticket panel.", 502)
        return web.json_response({
            "ok": True,
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "url": message.jump_url,
        })

    async def publish_self_roles_panel(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        _, guild = authorized
        cfg = self.guild_settings(self.state, guild.id)
        panel = cfg.get("selfrole_panel", {}) if isinstance(cfg, dict) else {}
        panel = panel if isinstance(panel, dict) else {}
        channel = self.text_channel(guild, panel.get("channel_id", 0))
        if channel is None:
            return self.json_error("Choose a valid self-role panel channel and save its settings first.", 400)
        me = guild.me
        if me is None:
            return self.json_error("KRYO is not ready in this server.", 503)
        if not me.guild_permissions.manage_roles:
            return self.json_error("KRYO needs Manage Roles to assign self roles.", 403)
        permissions = channel.permissions_for(me)
        if not permissions.send_messages or not permissions.embed_links:
            return self.json_error("KRYO needs Send Messages and Embed Links in the selected channel.", 403)
        cog_getter = getattr(self.bot, "get_cog", None)
        cog = cog_getter("Self Roles") if callable(cog_getter) else None
        if cog is None:
            return self.json_error("Self-role support is not available in this bot instance.", 503)
        try:
            message = await cog.publish_panel(
                guild,
                channel,
                title=panel.get("title", "Choose your roles"),
                description=panel.get(
                    "description",
                    "Choose any roles that fit you. Select a role again to remove it.",
                ),
            )
        except ValueError as error:
            return self.json_error(str(error), 400)
        except discord.Forbidden:
            return self.json_error("KRYO cannot publish a self-role panel in that channel.", 403)
        except discord.HTTPException as error:
            print(f"Self-role panel publish failed in guild {guild.id}: {type(error).__name__}: {error}")
            return self.json_error("Discord could not publish the self-role panel.", 502)
        return web.json_response({
            "ok": True,
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "url": message.jump_url,
        })

    async def manage_jail(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        session, guild = authorized
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException):
            return self.json_error("Request body must be valid JSON.", 400)
        if (
            not isinstance(payload, dict)
            or any(key not in {"action", "member_id", "reason"} for key in payload)
            or payload.get("action") not in {"jail", "unjail"}
        ):
            return self.json_error("Choose a valid jail action.", 400)
        raw_member_id = payload.get("member_id")
        if not isinstance(raw_member_id, (str, int)) or isinstance(raw_member_id, bool):
            return self.json_error("Enter a Discord username or member ID.", 400)
        member_identifier = str(raw_member_id).strip()
        if not member_identifier or len(member_identifier) > 100:
            return self.json_error("Enter a Discord username or member ID.", 400)
        if member_identifier.isdigit() and not 17 <= len(member_identifier) <= 20:
            return self.json_error("Enter a valid Discord member ID.", 400)
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 500:
            return self.json_error("The jail reason must be text with at most 500 characters.", 400)
        cog_getter = getattr(self.bot, "get_cog", None)
        cog = cog_getter("JailCog") if callable(cog_getter) else None
        if cog is None:
            return self.json_error("Jail management is not available in this bot instance.", 503)
        moderator = str(getattr(session, "user", {}).get("username", "Dashboard moderator"))
        try:
            if payload["action"] == "jail":
                member_reference: str | int = (
                    int(member_identifier) if member_identifier.isdigit() else member_identifier
                )
                await cog.jail_member(guild, member_reference, reason.strip(), moderator)
            else:
                if not member_identifier.isdigit():
                    return self.json_error("Release members by their Discord ID from the jailed member list.", 400)
                member_id = int(member_identifier)
                await cog.unjail_member(guild, int(member_id), moderator)
        except JailActionError as error:
            return self.json_error(str(error), error.status)
        return web.json_response({
            "ok": True,
            "action": payload["action"],
            "member_id": member_identifier,
        })

    async def music_action(self, request: web.Request) -> web.Response:
        authorized = await self.authorized_guild(request)
        if isinstance(authorized, web.Response):
            return authorized
        session, guild = authorized
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException):
            return self.json_error("Request body must be valid JSON.", 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            return self.json_error("Choose a valid music action.", 400)
        action = payload["action"]
        cog_getter = getattr(self.bot, "get_cog", None)
        cog = cog_getter("MusicPanel") if callable(cog_getter) else None
        if cog is None and callable(cog_getter):
            cog = cog_getter("Music")
        if cog is None:
            return self.json_error("Music is not available in this bot instance.", 503)
        try:
            member_id = int(str(session.user.get("id", "")))
        except (TypeError, ValueError):
            return self.json_error("The Discord session user is invalid.", 401)
        member = guild.get_member(member_id)
        if member is None:
            return self.json_error("Join this Discord server before using dashboard music controls.", 403)
        if action == "stay247":
            enabled = payload.get("enabled")
            if type(enabled) is not bool:
                return self.json_error("24/7 mode must be enabled or disabled.", 400)
            channel = None
            if enabled:
                try:
                    channel_id = int(str(payload.get("channel_id", "")))
                except (TypeError, ValueError):
                    return self.json_error("Choose a voice channel for 24/7 mode.", 400)
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    return self.json_error("Choose a valid voice channel from this server.", 400)
                bot_member = guild.me
                if bot_member is None:
                    return self.json_error("KRYO is not available in this Discord server.", 503)
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.connect or not permissions.speak:
                    return self.json_error("KRYO needs View Channel, Connect, and Speak permissions in that voice channel.", 403)
            persist = getattr(cog, "persist_247_settings", None)
            if not callable(persist):
                return self.json_error("Persistent 24/7 music settings are unavailable in this bot version.", 503)
            state = cog.state(guild.id)
            previous = (state.stay_247, state.stay_channel_id)
            state.stay_247 = enabled
            state.stay_channel_id = channel.id if channel is not None else None
            try:
                persist()
            except (RuntimeError, OSError) as error:
                state.stay_247, state.stay_channel_id = previous
                print(f"Dashboard could not save 24/7 music settings for guild {guild.id}: {type(error).__name__}: {error}")
                return self.json_error("Could not save 24/7 music settings. Try again.", 500)
            return web.json_response({
                "ok": True,
                "title": "24/7 Mode Updated",
                "description": "24/7 mode is on and saved for this server." if enabled else "24/7 mode is off and saved for this server.",
            })
        if action == "autoplay":
            enabled = payload.get("enabled")
            if type(enabled) is not bool:
                return self.json_error("Autoplay must be enabled or disabled.", 400)
            persist = getattr(cog, "persist_247_settings", None)
            if not hasattr(cog, "state") or not callable(persist):
                return self.json_error("Persistent music settings are unavailable in this bot version.", 503)
            state = cog.state(guild.id)
            previous = state.autoplay
            state.autoplay = enabled
            player = guild.voice_client
            previous_player_autoplay = getattr(player, "autoplay", None)
            if isinstance(player, wavelink.Player):
                player.autoplay = wavelink.AutoPlayMode.enabled if enabled else wavelink.AutoPlayMode.disabled
            try:
                persist()
            except (RuntimeError, OSError) as error:
                state.autoplay = previous
                if isinstance(player, wavelink.Player):
                    player.autoplay = previous_player_autoplay
                print(f"Dashboard could not save autoplay settings for guild {guild.id}: {type(error).__name__}: {error}")
                return self.json_error("Could not save autoplay settings. Try again.", 500)
            return web.json_response({
                "ok": True,
                "title": "Autoplay Updated",
                "description": f"Autoplay is {'on' if enabled else 'off'} and saved for this server.",
            })
        if action == "music_settings":
            post_now_playing = payload.get("post_now_playing")
            access_level = payload.get("access_level")
            if type(post_now_playing) is not bool:
                return self.json_error("Now-playing cards must be enabled or disabled.", 400)
            if not isinstance(access_level, str) or access_level not in {"everyone", "dj", "admin"}:
                return self.json_error("Choose a valid music access level.", 400)
            try:
                panel_channel_id = int(str(payload.get("panel_channel_id") or 0))
                dj_role_id = int(str(payload.get("dj_role_id") or 0))
            except (TypeError, ValueError):
                return self.json_error("Choose valid music channel and role settings.", 400)
            panel_channel = guild.get_channel(panel_channel_id) if panel_channel_id else None
            if panel_channel_id and not isinstance(panel_channel, discord.TextChannel):
                return self.json_error("Choose a text channel from this server for music cards.", 400)
            if panel_channel is not None:
                bot_member = guild.me
                if bot_member is None:
                    return self.json_error("KRYO is not available in this Discord server.", 503)
                permissions = panel_channel.permissions_for(bot_member)
                if not all((
                    permissions.view_channel,
                    permissions.send_messages,
                    permissions.embed_links,
                    permissions.attach_files,
                )):
                    return self.json_error("KRYO needs View Channel, Send Messages, Embed Links, and Attach Files in the card channel.", 403)
            dj_role = guild.get_role(dj_role_id) if dj_role_id else None
            if dj_role_id and dj_role is None:
                return self.json_error("Choose a DJ role from this server.", 400)
            if access_level == "dj" and dj_role is None:
                return self.json_error("Select a DJ role before limiting music to DJs.", 400)
            persist = getattr(cog, "persist_247_settings", None)
            if not callable(persist) or not hasattr(cog, "state"):
                return self.json_error("Persistent music settings are unavailable in this bot version.", 503)
            state = cog.state(guild.id)
            previous = (
                state.post_now_playing,
                state.text_channel_id,
                state.access_level,
                state.dj_role_id,
            )
            state.post_now_playing = post_now_playing
            state.text_channel_id = panel_channel_id or None
            state.access_level = access_level
            state.dj_role_id = dj_role_id or None
            try:
                persist()
            except (RuntimeError, OSError) as error:
                (
                    state.post_now_playing,
                    state.text_channel_id,
                    state.access_level,
                    state.dj_role_id,
                ) = previous
                print(f"Dashboard could not save music settings for guild {guild.id}: {type(error).__name__}: {error}")
                return self.json_error("Could not save music settings. Try again.", 500)
            remove_panel = getattr(cog, "remove_panel", None)
            if callable(remove_panel) and (
                not post_now_playing
                or previous[1] != state.text_channel_id
            ):
                await remove_panel(state)
            if post_now_playing and isinstance(guild.voice_client, wavelink.Player):
                await cog.send_panel(guild.voice_client, state)
            return web.json_response({
                "ok": True,
                "title": "Music Settings Updated",
                "description": "Music settings saved for this server.",
            })
        try:
            if action in {"join", "move", "leave"}:
                if action == "leave":
                    player = guild.voice_client
                    if player is None:
                        return self.json_error("KRYO is not connected to a voice channel.", 400)
                    await player.disconnect()
                    return web.json_response({"ok": True, "title": "Voice Disconnected", "description": "KRYO left the voice channel."})
                try:
                    channel_id = int(str(payload.get("channel_id", "")))
                except (TypeError, ValueError):
                    return self.json_error("Choose a valid voice channel.", 400)
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    return self.json_error("Choose a valid voice channel from this server.", 400)
                bot_member = guild.me
                if bot_member is None:
                    return self.json_error("KRYO is not available in this Discord server.", 503)
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.connect or not permissions.speak:
                    return self.json_error("KRYO needs View Channel, Connect, and Speak permissions in that voice channel.", 403)
                player = guild.voice_client
                if player is not None:
                    if not isinstance(player, wavelink.Player):
                        return self.json_error("Another voice session is active in this server.", 409)
                    await player.move_to(channel)
                else:
                    player = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=30)
                state_getter = getattr(cog, "state", None)
                if isinstance(player, wavelink.Player) and callable(state_getter):
                    music_state = state_getter(guild.id)
                    player.autoplay = (
                        wavelink.AutoPlayMode.enabled
                        if getattr(music_state, "autoplay", False)
                        else wavelink.AutoPlayMode.disabled
                    )
                return web.json_response({
                    "ok": True,
                    "title": "Voice Connected",
                    "description": f"KRYO joined {channel.name}.",
                })
            if action in {"play", "queue_add"}:
                query = payload.get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 500:
                    return self.json_error("Enter a song name or link.", 400)
                if hasattr(cog, "play_query"):
                    if guild.voice_client is None and payload.get("channel_id"):
                        try:
                            channel_id = int(str(payload["channel_id"]))
                        except (TypeError, ValueError):
                            return self.json_error("Choose a valid voice channel.", 400)
                        music_state_getter = getattr(cog, "state", None)
                        if callable(music_state_getter):
                            preferences = music_state_getter(guild.id)
                            if preferences.stay_247 and preferences.stay_channel_id:
                                channel_id = preferences.stay_channel_id
                        channel = guild.get_channel(channel_id)
                        if not isinstance(channel, discord.VoiceChannel):
                            return self.json_error("Choose a valid voice channel from this server.", 400)
                        if guild.me is None:
                            return self.json_error("KRYO is not available in this Discord server.", 503)
                        permissions = channel.permissions_for(guild.me)
                        if not permissions.view_channel or not permissions.connect or not permissions.speak:
                            return self.json_error("KRYO needs View Channel, Connect, and Speak permissions in that voice channel.", 403)
                        await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=30)
                    result = await cog.play_query(guild, member, guild.voice_client.channel if guild.voice_client else None, query.strip())
                    if isinstance(result, tuple):
                        return self.json_error("This Spotify link can show a preview but cannot be played by the bot. Search for the track by name instead.", 400)
                    if isinstance(result, str) and result.startswith(("Added ", "Loaded ")):
                        return web.json_response({
                            "ok": True,
                            "title": "Track Queued",
                            "description": result,
                        })
                    if isinstance(result, str):
                        return self.json_error(result, 400)
                    return web.json_response({
                        "ok": True,
                        "title": "Playback Started",
                        "description": "The requested track was sent to the player.",
                    })
                embed = await cog.do_play(guild, member, query.strip())
            elif hasattr(cog, "play_query") and hasattr(cog, "state"):
                player = guild.voice_client
                if not isinstance(player, wavelink.Player):
                    return self.json_error("KRYO is not connected to a voice channel.", 400)
                music_state = cog.state(guild.id)
                success_description = "Music action completed."
                if action == "pause":
                    result = await cog.do_pause(player, music_state)
                elif action == "resume":
                    result = await cog.do_resume(player, music_state)
                elif action == "skip":
                    result = await cog.do_skip(player, music_state)
                elif action == "prev":
                    result = await cog.do_prev(player, music_state)
                elif action == "stop":
                    result = await cog.do_stop(player, music_state)
                elif action == "queue":
                    current = player.current
                    queued = list(player.queue)
                    description = current.title if current is not None else "Nothing is playing."
                    if queued:
                        description += "\n" + "\n".join(f"{index}. {track.title}" for index, track in enumerate(queued[:20], 1))
                    return web.json_response({"ok": True, "title": "Music Queue", "description": description})
                elif action == "nowplaying":
                    current = player.current
                    if current is None:
                        return self.json_error("Nothing is playing right now.", 400)
                    return web.json_response({"ok": True, "title": current.title, "description": current.author or "Unknown"})
                elif action == "lyrics":
                    if player.current is None:
                        return self.json_error("Nothing is playing right now.", 400)
                    lyrics = await cog.lyrics_embed(player.current)
                    return web.json_response({
                        "ok": True,
                        "title": str(lyrics.title or "Lyrics"),
                        "description": str(lyrics.description or "No lyrics were found."),
                    })
                elif action == "volume":
                    level = payload.get("level")
                    if type(level) is not int or not 0 <= level <= 150:
                        return self.json_error("Volume must be a whole number from 0 to 150.", 400)
                    await cog.set_volume(player, level)
                    success_description = f"Volume set to {level}%."
                elif action == "loop":
                    mode = payload.get("mode")
                    if not isinstance(mode, str) or mode not in {"off", "track", "queue"}:
                        return self.json_error("Choose off, track, or queue for loop mode.", 400)
                    music_state.loop = mode
                    success_description = f"Loop mode set to {mode}."
                elif action == "shuffle":
                    result = await cog.do_shuffle(player, music_state)
                elif action == "clear":
                    player.queue.clear()
                    success_description = "Music queue cleared."
                elif action == "remove":
                    position = payload.get("position")
                    if type(position) is not int or position < 1:
                        return self.json_error("Queue position must be a positive number.", 400)
                    if position > len(player.queue):
                        return self.json_error("That queue position no longer exists.", 400)
                    player.queue.delete(position - 1)
                    success_description = f"Removed track {position} from the queue."
                elif action in {"queue_move", "queue_play"}:
                    position = payload.get("position")
                    direction = payload.get("direction", 0)
                    if type(position) is not int or not 1 <= position <= len(player.queue):
                        return self.json_error("That queue position no longer exists.", 400)
                    track = player.queue[position - 1]
                    if action == "queue_move":
                        if type(direction) is not int or direction not in {-1, 1}:
                            return self.json_error("Choose a valid queue direction.", 400)
                        target = position - 1 + direction
                        if not 0 <= target < len(player.queue):
                            return self.json_error("That track cannot move farther in that direction.", 400)
                        player.queue.delete(position - 1)
                        player.queue.put_at(target, track)
                        success_description = "Queue order updated."
                    else:
                        player.queue.delete(position - 1)
                        async with music_state.lock:
                            if player.current is not None:
                                player.queue.put_at(0, player.current)
                            if not await cog.start_track(player, music_state, track):
                                return self.json_error("Could not start the selected track.", 502)
                        success_description = f"Now playing {track.title}."
                elif action == "seek":
                    seconds = payload.get("seconds")
                    current = player.current
                    if type(seconds) is not int or seconds < 0:
                        return self.json_error("Seek position must be a non-negative whole number of seconds.", 400)
                    if current is None or current.is_stream or not current.is_seekable:
                        return self.json_error("The current track cannot be seeked.", 400)
                    target = min(seconds * 1000, max(0, current.length - 1000))
                    await player.seek(target)
                    music_state.offset_ms = target
                    success_description = f"Playback moved to {target // 1000} seconds."
                elif action == "filter":
                    value = payload.get("value", "normal")
                    if not isinstance(value, str) or value not in {"normal", "bassboost", "nightcore", "vaporwave", "8d", "karaoke"}:
                        return self.json_error("Choose a valid audio filter.", 400)
                    await player.set_filters(build_filters(value))
                    music_state.filter = value
                    success_description = f"Audio filter set to {value}."
                else:
                    return self.json_error("Unsupported music action.", 400)
                if isinstance(result, str) and result and not (action == "stop" and result.startswith("Stopped")):
                    return self.json_error(result, 400)
                if action == "stop" and isinstance(result, str):
                    success_description = result
                return web.json_response({"ok": True, "title": "Music Updated", "description": success_description})
            elif action == "pause":
                embed = await cog.do_pause(guild, member)
            elif action == "resume":
                embed = await cog.do_resume(guild, member)
            elif action == "skip":
                embed = await cog.do_skip(guild, member)
            elif action == "stop":
                embed = await cog.do_stop(guild, member)
            elif action == "queue":
                embed = await cog.do_queue(guild, member)
            elif action == "nowplaying":
                embed = await cog.do_nowplaying(guild, member)
            elif action == "volume":
                level = payload.get("level")
                if not isinstance(level, int) or isinstance(level, bool):
                    return self.json_error("Volume must be a number from 0 to 150.", 400)
                embed = await cog.do_volume(guild, member, level)
            elif action == "loop":
                mode = payload.get("mode")
                if not isinstance(mode, str) or mode not in {"off", "track", "queue"}:
                    return self.json_error("Choose off, track, or queue for loop mode.", 400)
                embed = await cog.do_loop(guild, member, mode)
            elif action == "shuffle":
                embed = await cog.do_shuffle(guild, member)
            elif action == "clear":
                embed = await cog.do_clear(guild, member)
            elif action == "remove":
                position = payload.get("position")
                if type(position) is not int or position < 1:
                    return self.json_error("Queue position must be a positive number.", 400)
                embed = await cog.do_remove(guild, member, position)
            elif action == "seek":
                seconds = payload.get("seconds")
                if type(seconds) is not int:
                    return self.json_error("Seek position must be a whole number of seconds.", 400)
                embed = await cog.do_seek(guild, member, seconds)
            elif action == "filter":
                value = payload.get("value", "normal")
                if not isinstance(value, str) or value not in {"normal", "bassboost", "nightcore", "vaporwave", "8d", "karaoke"}:
                    return self.json_error("Choose a valid audio filter.", 400)
                player, problem = cog.get_control(guild, member, need_current=False)
                if problem is not None:
                    embed = problem
                else:
                    await player.set_filters(build_filters(value))
                    embed = discord.Embed(title="Filter Updated", description=f"Audio filter set to **{value}**.")
            else:
                return self.json_error("Unsupported music action.", 400)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Dashboard music action failed in guild {guild.id}: {type(error).__name__}: {error}")
            return self.json_error("Discord could not complete that music action.", 502)
        except Exception as error:
            print(f"Dashboard music action failed in guild {guild.id}: {type(error).__name__}: {error}")
            return self.json_error("The music action could not be completed.", 502)
        if embed is None:
            return self.json_error("The music action returned no result.", 502)
        if embed.color is not None and embed.color.value in {ui.COLORS["error"], ui.COLORS["warn"]}:
            return self.json_error(str(embed.description or embed.title or "The music action could not be completed."), 400)
        return web.json_response({
            "ok": True,
            "title": str(getattr(embed, "title", "") or ""),
            "description": str(getattr(embed, "description", "") or ""),
        })

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    @staticmethod
    def default_invite_tracker_payload(
        guild: discord.Guild,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        cfg = settings.get("invite_tracker", {})
        cfg = cfg if isinstance(cfg, dict) else {}
        me = guild.me
        minimum_age = cfg.get("minimum_account_age_days", 7)
        if type(minimum_age) is not int or not 0 <= minimum_age <= 90:
            minimum_age = 7
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "log_channel_id": str(cfg.get("log_channel_id", 0) or ""),
            "minimum_account_age_days": minimum_age,
            "tracking_available": bool(me and me.guild_permissions.manage_guild),
            "your_invites": {"active": 0, "left": 0, "fake": 0, "total": 0},
            "leaderboard": [],
        }

    @staticmethod
    def text_channel(guild: discord.Guild, raw_id: Any) -> discord.TextChannel | None:
        if raw_id in (None, "", 0, "0"):
            return None
        try:
            channel = guild.get_channel(int(raw_id))
        except (TypeError, ValueError):
            return None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def role(guild: discord.Guild, raw_id: Any) -> discord.Role | None:
        try:
            return guild.get_role(int(raw_id))
        except (TypeError, ValueError):
            return None

    async def options(self, _request: web.Request) -> web.Response:
        return web.Response(status=204)
