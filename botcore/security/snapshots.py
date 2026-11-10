"""Automatic snapshots of server configuration, and restoring from them.

Audit logs are the only *live* record of what something used to look like, and
they expire. They also only describe the single most recent change, so they
cannot answer "what did this role look like three days ago". This module keeps
its own trusted copy in the shared state blob so a restore does not depend on an
audit entry still being there.

What is stored, and why Discord makes the rest impossible:

* roles - full permission bitfield, colour, hoist, mentionable, position
* channels - name, type, category, topic, overwrites, slowmode, bitrate
* emojis / stickers - identity and name only. Discord never hands back the
  original image bytes, so an asset can be *detected* and the offender punished
  but the file itself cannot be re-uploaded.
* server settings - the ones the bot is allowed to read back.

Everything is JSON-safe so the whole thing survives the existing single-row
SQLite blob without a schema migration.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional

import discord

SNAPSHOT_VERSION = 2
#: How many snapshots to keep per guild. Each is small, but a long-running bot
#: on many servers should not grow the database without bound.
MAX_SNAPSHOTS = 10
#: Overwrites are stored per channel; a server with thousands of channels makes
#: a snapshot expensive, so cap what one snapshot stores.
MAX_CHANNELS = 400
MAX_ROLES = 250
MAX_EMOJI = 500
MAX_STICKER = 200

RESTORED = "restored"
PARTIAL = "partial"
FAILED = "failed"
SKIPPED = "skipped"


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ capture
def enum_value(value: Any) -> Optional[int]:
    """Raw integer behind a Discord enum.

    `discord.VerificationLevel` and friends are enum members that refuse
    `int()`, so reading them this way is the only way that works across
    discord.py versions.
    """
    raw = getattr(value, "value", value)
    return _as_int(raw)


def seconds_value(value: Any) -> int:
    """Seconds that may arrive as an int or a timedelta.

    `Guild.afk_timeout` is a plain int in discord.py 2.x, but
    older versions and test doubles hand out a timedelta.
    """
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    return _as_int(value, 0) or 0


def capture_role(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": int(role.permissions.value),
        "color": int(role.colour.value or 0),
        "hoist": bool(role.hoist),
        "mentionable": bool(role.mentionable),
        "position": role.position,
    }


def capture_overwrites(channel: Any) -> list[dict[str, Any]]:
    """Overwrites as plain dicts keyed by target id and kind (0 role, 1 member)."""
    stored: list[dict[str, Any]] = []
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        allow, deny = overwrite.pair()
        stored.append(
            {
                "id": getattr(target, "id", 0),
                "type": 1 if isinstance(target, discord.Member) else 0,
                # `pair()` hands back Permissions objects, and PermissionOverwrite
                # sometimes returns plain ints, so normalise to the raw bit.
                "allow": int(getattr(allow, "value", allow) or 0),
                "deny": int(getattr(deny, "value", deny) or 0),
                "sync": bool(getattr(overwrite, "sync", False)),
            }
        )
    return stored


def capture_channel(channel: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type.name if hasattr(channel.type, "name") else str(channel.type),
        "category_id": getattr(channel, "category_id", None),
        "position": getattr(channel, "position", 0),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "overwrites": capture_overwrites(channel),
    }
    if isinstance(channel, discord.TextChannel):
        snapshot["topic"] = (channel.topic or "")[:1024]
        snapshot["slowmode"] = channel.slowmode_delay
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        snapshot["bitrate"] = channel.bitrate
        snapshot["user_limit"] = channel.user_limit
        snapshot["rtc_region"] = channel.rtc_region.name if channel.rtc_region else None
    if isinstance(channel, discord.ForumChannel):
        snapshot["topic"] = (channel.topic or "")[:1024]
        snapshot["slowmode"] = channel.slowmode_delay
        try:
            snapshot["available_tags"] = [
                {"name": tag.name, "moderated": tag.moderated, "emoji_id": tag.emoji.id if tag.emoji else None}
                for tag in channel.available_tags
            ]
        except (AttributeError, discord.HTTPException):
            snapshot["available_tags"] = []
    return snapshot


def capture_emoji(emoji: Any) -> dict[str, Any]:
    """Identity only. Discord will not give the original bytes back."""
    return {
        "id": emoji.id,
        "name": emoji.name,
        "animated": bool(getattr(emoji, "animated", False)),
        "managed": bool(getattr(emoji, "managed", False)),
        "url": str(getattr(emoji, "url", "") or ""),
    }


def capture_sticker(sticker: Any) -> dict[str, Any]:
    return {
        "id": sticker.id,
        "name": sticker.name,
        "description": (getattr(sticker, "description", "") or "")[:100],
        "tags": getattr(sticker, "tags", ""),
        "url": str(getattr(sticker, "url", "") or ""),
    }


def capture_guild(guild: discord.Guild, *, emojis: Iterable[Any] = (), stickers: Iterable[Any] = ()) -> dict[str, Any]:
    """One full, JSON-safe snapshot of a server."""
    roles = sorted((capture_role(role) for role in guild.roles), key=lambda item: item["position"])
    channels = [capture_channel(channel) for channel in guild.channels]
    channels.sort(key=lambda item: item.get("position") or 0)
    return {
        "version": SNAPSHOT_VERSION,
        "guild_id": guild.id,
        "guild_name": guild.name,
        "created_at": time.time(),
        "roles": roles[:MAX_ROLES],
        "channels": channels[:MAX_CHANNELS],
        "emojis": [capture_emoji(emoji) for emoji in emojis][:MAX_EMOJI],
        "stickers": [capture_sticker(sticker) for sticker in stickers][:MAX_STICKER],
        "settings": {
            "name": guild.name,
            "description": guild.description or "",
            "verification_level": enum_value(guild.verification_level),
            "explicit_content_filter": enum_value(guild.explicit_content_filter),
            "default_notifications": enum_value(guild.default_notifications),
            "afk_timeout": seconds_value(guild.afk_timeout),
            "preferred_locale": str(guild.preferred_locale or ""),
            "system_channel_id": guild.system_channel_id,
            "afk_channel_id": guild.afk_channel_id,
            "rules_channel_id": guild.rules_channel_id,
            "public_updates_channel_id": guild.public_updates_channel_id,
            "safety_alerts_channel_id": getattr(guild, "safety_alerts_channel_id", None),
            "vanity_url_code": guild.vanity_url_code,
            "icon_hash": str(guild.icon.key) if getattr(guild, "icon", None) else None,
        },
    }


async def capture(guild: discord.Guild) -> dict[str, Any]:
    """Snapshot including emoji and stickers, fetched from the API."""
    emojis: list[Any] = []
    stickers: list[Any] = []
    try:
        emojis = await guild.fetch_emojis()
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        emojis = list(getattr(guild, "emojis", []) or [])
    try:
        stickers = await guild.fetch_stickers()
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        stickers = list(getattr(guild, "stickers", []) or [])
    return capture_guild(guild, emojis=emojis, stickers=stickers)


# ------------------------------------------------------------------- storage
class SnapshotStore:
    """Keeps the snapshot history inside the shared persisted state dict."""

    def __init__(self, settings_for: Any, save: Any) -> None:
        # settings_for(guild_id) -> the guild's settings sub-dict
        self._settings_for = settings_for
        self._save = save

    def history(self, guild_id: int) -> list[dict[str, Any]]:
        settings = self._settings_for(guild_id)
        store = settings.setdefault("snapshots", {})
        items = store.get(str(guild_id)) or []
        return items if isinstance(items, list) else []

    def add(self, guild_id: int, snapshot: dict[str, Any], *, label: str = "") -> dict[str, Any]:
        snapshot = dict(snapshot)
        snapshot["label"] = label or "automatic"
        items = self.history(guild_id)
        items.append(snapshot)
        # Keep the newest few, but never drop an admin-pinned one.
        pinned = [item for item in items if item.get("pinned")]
        rest = [item for item in items if not item.get("pinned")]
        rest = rest[-(MAX_SNAPSHOTS - len(pinned)) :] if len(pinned) < MAX_SNAPSHOTS else rest[-1:]
        self._settings_for(guild_id).setdefault("snapshots", {})[str(guild_id)] = pinned + rest
        self._save()
        return snapshot

    def latest(self, guild_id: int) -> Optional[dict[str, Any]]:
        items = self.history(guild_id)
        return items[-1] if items else None

    def by_id(self, guild_id: int, stamp: float) -> Optional[dict[str, Any]]:
        for item in reversed(self.history(guild_id)):
            if abs(float(item.get("created_at", 0)) - float(stamp)) < 0.5:
                return item
        return None

    def pin(self, guild_id: int, stamp: float) -> bool:
        target = self.by_id(guild_id, stamp)
        if target is None:
            return False
        for item in self.history(guild_id):
            item["pinned"] = False
        target["pinned"] = True
        self._save()
        return True

    def clear(self, guild_id: int) -> None:
        self._settings_for(guild_id).setdefault("snapshots", {}).pop(str(guild_id), None)
        self._save()


# ------------------------------------------------------------------- diffing
def diff(snapshot: dict[str, Any], guild: discord.Guild) -> dict[str, list[Any]]:
    """What changed between a snapshot and the live server right now."""
    result: dict[str, list[Any]] = {"roles": [], "channels": [], "emojis": [], "stickers": [], "settings": []}
    if not snapshot:
        return result

    live_roles = {role.id: role for role in guild.roles}
    for stored in snapshot.get("roles", []):
        role = live_roles.get(stored["id"])
        if role is None:
            result["roles"].append({"id": stored["id"], "kind": "missing", "stored": stored})
            continue
        changes: dict[str, Any] = {}
        if int(role.permissions.value) != stored.get("permissions"):
            changes["permissions"] = {"old": stored.get("permissions"), "new": int(role.permissions.value)}
        if int(role.colour.value or 0) != stored.get("color"):
            changes["color"] = {"old": stored.get("color"), "new": int(role.colour.value or 0)}
        if role.hoist != stored.get("hoist"):
            changes["hoist"] = {"old": stored.get("hoist"), "new": role.hoist}
        if role.name != stored.get("name"):
            changes["name"] = {"old": stored.get("name"), "new": role.name}
        if changes:
            result["roles"].append({"id": stored["id"], "kind": "changed", "changes": changes})

    live_channels = {channel.id: channel for channel in guild.channels}
    for stored in snapshot.get("channels", []):
        channel = live_channels.get(stored["id"])
        if channel is None:
            result["channels"].append({"id": stored["id"], "name": stored.get("name"), "kind": "missing"})
            continue
        changes: dict[str, Any] = {}
        if channel.name != stored.get("name"):
            changes["name"] = {"old": stored.get("name"), "new": channel.name}
        if getattr(channel, "category_id", None) != stored.get("category_id"):
            changes["category_id"] = {"old": stored.get("category_id"), "new": getattr(channel, "category_id", None)}
        if _overwrite_value(channel) != stored.get("overwrites"):
            changes["overwrites"] = {"old": stored.get("overwrites"), "new": capture_overwrites(channel)}
        if changes:
            result["channels"].append({"id": stored["id"], "name": channel.name, "kind": "changed", "changes": changes})

    live_emojis = {emoji.id for emoji in getattr(guild, "emojis", []) or []}
    for stored in snapshot.get("emojis", []):
        if stored["id"] not in live_emojis and not stored.get("managed"):
            result["emojis"].append(stored)

    live_stickers = {sticker.id for sticker in getattr(guild, "stickers", []) or []}
    for stored in snapshot.get("stickers", []):
        if stored["id"] not in live_stickers:
            result["stickers"].append(stored)

    stored_settings = snapshot.get("settings", {})
    live_settings = capture_guild(guild).get("settings", {})
    for key, old in stored_settings.items():
        new = live_settings.get(key)
        if old != new and key not in ("vanity_url_code", "icon_hash"):
            result["settings"].append({"key": key, "old": old, "new": new})
    return result


def _overwrite_value(channel: Any) -> list[dict[str, Any]]:
    items = capture_overwrites(channel)
    return sorted(items, key=lambda item: (item["type"], item["id"]))


# ------------------------------------------------------------------ restoring
class RestoreReport:
    """Outcome of one restore run, suitable for the security log."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.done: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []

    @property
    def status(self) -> str:
        if self.failed and not self.done:
            return FAILED
        if self.failed or self.skipped:
            return PARTIAL
        return RESTORED

    def ok(self, note: str) -> None:
        self.done.append(note)

    def fail(self, note: str) -> None:
        self.failed.append(note)

    def skip(self, note: str) -> None:
        self.skipped.append(note)

    def summary(self) -> str:
        parts = []
        if self.done:
            parts.append(f"{len(self.done)} restored")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts) or "nothing to do"

    def lines(self) -> str:
        rows = []
        for note in self.done[:20]:
            rows.append(f"{_MARK} {note}")
        for note in self.skipped[:20]:
            rows.append(f"{_SKIP_MARK} {note}")
        for note in self.failed[:20]:
            rows.append(f"{_FAIL_MARK} {note}")
        return "\n".join(rows)


_MARK = "\U0001F7E2"
_SKIP_MARK = "\U0001F7E1"
_FAIL_MARK = "\U0001F534"


async def restore_roles(guild: discord.Guild, snapshot: dict[str, Any], report: RestoreReport, reason: str) -> None:
    """Put back missing roles and undo permission changes on existing ones."""
    live = {role.id: role for role in guild.roles}
    ordered = sorted(snapshot.get("roles", []), key=lambda item: item.get("position", 0))
    for stored in ordered:
        # @everyone can never be recreated and its permissions live on the guild.
        if stored.get("id") == guild.id:
            continue
        role = live.get(stored["id"])
        if role is None:
            if role is None and any(existing.name == stored["name"] for existing in guild.roles):
                report.skip(f"role **{stored['name']}** could not be restored, a role with that name exists")
                continue
            if role is not None:
                continue
            try:
                created = await guild.create_role(
                    name=stored["name"],
                    permissions=discord.Permissions(stored.get("permissions", 0)),
                    colour=discord.Colour(stored.get("color", 0)),
                    hoist=bool(stored.get("hoist")),
                    mentionable=bool(stored.get("mentionable")),
                    reason=reason,
                )
                report.ok(f"recreated the role **{stored['name']}**")
                if stored.get("position"):
                    try:
                        await created.edit(position=max(1, int(stored["position"])), reason=reason)
                    except (discord.Forbidden, discord.HTTPException):
                        report.skip(f"role **{stored['name']}** is back but not at its original position")
            except discord.Forbidden as error:
                report.fail(f"role **{stored['name']}**: missing permission ({error.status})")
            except (discord.HTTPException, TypeError, ValueError) as error:
                report.fail(f"role **{stored['name']}**: {type(error).__name__}")
            continue

        options: dict[str, Any] = {}
        if int(role.permissions.value) != stored.get("permissions"):
            options["permissions"] = discord.Permissions(stored.get("permissions", 0))
        if int(role.colour.value or 0) != stored.get("color"):
            options["colour"] = discord.Colour(stored.get("color", 0))
        if role.hoist != stored.get("hoist"):
            options["hoist"] = bool(stored.get("hoist"))
        if role.name != stored.get("name"):
            options["name"] = stored["name"]
        if not options:
            continue
        try:
            await role.edit(reason=reason, **options)
            report.ok(f"restored permissions of **{role.name}**")
        except discord.Forbidden:
            report.fail(f"role **{role.name}** is above my highest role")
        except (discord.HTTPException, TypeError, ValueError) as error:
            report.fail(f"role **{role.name}**: {type(error).__name__}")


async def restore_channels(guild: discord.Guild, snapshot: dict[str, Any], report: RestoreReport, reason: str) -> None:
    """Recreate deleted channels and fix renamed or re-permissioned ones."""
    live = {channel.id: channel for channel in guild.channels}
    stored_list = sorted(snapshot.get("channels", []), key=lambda item: item.get("position", 0))
    # Recreate first so later overwrites can resolve their category.
    for stored in stored_list:
        if stored["id"] in live:
            continue
        category = guild.get_channel(stored.get("category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = None
        try:
            created = await _create_channel_like(guild, stored, category, reason)
        except discord.Forbidden as error:
            report.fail(f"#{stored['name']}: missing Manage Channels ({error.status})")
            continue
        except (discord.HTTPException, TypeError, ValueError) as error:
            report.fail(f"#{stored['name']}: {type(error).__name__}")
            continue
        if created is None:
            report.skip(f"#{stored['name']} has a channel type this bot cannot create")
            continue
        applied = await apply_overwrites(guild, created, stored.get("overwrites", []), reason)
        note = f"recreated #{stored['name']}"
        if applied:
            note += f" with {applied} overwrite(s)"
        report.ok(note + ".")

    for stored in stored_list:
        channel = live.get(stored["id"])
        if channel is None:
            continue
        options: dict[str, Any] = {}
        if channel.name != stored.get("name"):
            options["name"] = stored["name"]
        if isinstance(channel, discord.TextChannel):
            if (channel.topic or "")[:1024] != stored.get("topic", ""):
                options["topic"] = stored.get("topic", "")
            if channel.slowmode_delay != stored.get("slowmode"):
                options["slowmode"] = stored.get("slowmode", 0)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            if channel.bitrate != stored.get("bitrate"):
                options["bitrate"] = stored.get("bitrate")
            if channel.user_limit != stored.get("user_limit"):
                options["user_limit"] = stored.get("user_limit", 0)
        if options:
            try:
                await channel.edit(reason=reason, **options)
                report.ok(f"restored the settings of #{stored['name']}")
            except discord.Forbidden:
                report.fail(f"#{stored['name']}: missing Manage Channels")
            except (discord.HTTPException, TypeError, ValueError) as error:
                report.fail(f"#{stored['name']}: {type(error).__name__}")

        if _overwrite_value(channel) != stored.get("overwrites"):
            applied = await apply_overwrites(guild, channel, stored.get("overwrites", []), reason, replace=True)
            if applied:
                report.ok(f"restored {applied} permission overwrite(s) in #{stored['name']}")
            else:
                report.fail(f"could not restore the overwrites of #{stored['name']}")


async def _create_channel_like(guild: discord.Guild, stored: dict[str, Any], category, reason: str):
    kind = str(stored.get("type") or "text")
    name = stored.get("name") or "restored"
    topic = stored.get("topic", "")
    if kind == "category":
        return await guild.create_category(name, reason=reason)
    if kind == "voice":
        return await guild.create_voice_channel(
            name,
            category=category,
            bitrate=stored.get("bitrate", 64000),
            user_limit=stored.get("user_limit", 0),
            reason=reason,
        )
    if kind == "stage":
        return await guild.create_stage_channel(name, category=category, reason=reason)
    if kind == "forum":
        try:
            return await guild.create_forum(
                name, topic=topic or None, category=category, reason=reason
            )
        except (AttributeError, discord.HTTPException, TypeError):
            # Forums cannot be created by older library versions; a text channel
            # with the same name is closer to the original than nothing.
            return await guild.create_text_channel(name, category=category, topic=topic or None, reason=reason)
    return await guild.create_text_channel(name, category=category, topic=topic or None, reason=reason)


async def apply_overwrites(
    guild: discord.Guild, channel: Any, stored: Iterable[dict[str, Any]], reason: str, *, replace: bool = False
) -> int:
    """Push stored overwrites onto a live channel. Returns how many were set."""
    if replace:
        try:
            await channel.edit(overwrites={}, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            return 0
    applied = 0
    for item in stored:
        target = guild.get_member(item["id"]) if item.get("type") == 1 else guild.get_role(item["id"])
        if target is None:
            # A member who left the server cannot be targeted; skip quietly.
            continue
        try:
            overwrite = discord.PermissionOverwrite.from_pair(
                discord.Permissions(item.get("allow", 0)),
                discord.Permissions(item.get("deny", 0)),
            )
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
            applied += 1
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            continue
    return applied


async def restore_settings(guild: discord.Guild, snapshot: dict[str, Any], report: RestoreReport, reason: str) -> None:
    """Roll back the server settings Discord lets a bot write."""
    stored = snapshot.get("settings", {})
    if not stored:
        return
    options: dict[str, Any] = {}
    if stored.get("name") and stored["name"] != guild.name:
        options["name"] = stored["name"]
    if stored.get("description", "") != (guild.description or ""):
        options["description"] = stored.get("description", "")
    if stored.get("verification_level") is not None and stored["verification_level"] != enum_value(guild.verification_level):
        options["verification_level"] = discord.VerificationLevel(stored["verification_level"])
    if stored.get("explicit_content_filter") is not None:
        options["explicit_content_filter"] = discord.ContentFilter(stored["explicit_content_filter"])
    if stored.get("default_notifications") is not None:
        options["default_notifications"] = discord.NotificationLevel(stored["default_notifications"])
    if stored.get("afk_timeout"):
        pause = timedelta_from(stored["afk_timeout"])
        if pause is not None:
            options["afk_timeout"] = pause
    for key, attribute in (
        ("system_channel_id", "system_channel"),
        ("afk_channel_id", "afk_channel"),
        ("rules_channel_id", "rules_channel"),
        ("public_updates_channel_id", "public_updates_channel"),
    ):
        if stored.get(key) is None:
            continue
        channel = guild.get_channel(stored[key])
        wanted = discord.TextChannel if attribute in ("system_channel", "rules_channel", "public_updates_channel") else discord.VoiceChannel
        if isinstance(channel, wanted) and getattr(channel, "id", None) != getattr(getattr(guild, attribute), "id", None):
            options[attribute] = channel
    if not options:
        return
    try:
        await guild.edit(reason=reason, **options)
        report.ok("restored the server settings: " + ", ".join(sorted(options)))
    except discord.Forbidden as error:
        report.fail(f"server settings: missing Manage Server ({error.status})")
    except (discord.HTTPException, TypeError, ValueError) as error:
        report.fail(f"server settings: {type(error).__name__}")


def timedelta_from(seconds: Any):
    from datetime import timedelta

    try:
        return timedelta(seconds=int(seconds))
    except (TypeError, ValueError):
        return None


async def restore_icon(guild: discord.Guild, snapshot: dict[str, Any], report: RestoreReport, reason: str) -> None:
    """Put the old icon back by downloading the hash that is already stored."""
    icon_hash = (snapshot.get("settings") or {}).get("icon_hash")
    if not icon_hash:
        return
    if guild.icon is not None and str(guild.icon.key) == str(icon_hash):
        return
    data = await _download(f"https://cdn.discordapp.com/icons/{guild.id}/{icon_hash}.png")
    if not data:
        # Discord does not expose the bytes without the hash, so this is a hard
        # limit rather than a bug: report it honestly instead of pretending.
        report.skip("the server icon could not be restored because its original file is no longer available")
        return
    import io

    try:
        await guild.edit(icon=io.BytesIO(data), reason=reason)
        report.ok("restored the server icon")
    except (discord.Forbidden, discord.HTTPException):
        report.fail("the server icon could not be restored (missing Manage Server)")


async def _download(url: str) -> Optional[bytes]:
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.read()
    except (aiohttp.ClientError, OSError):
        return None


async def restore_all(guild: discord.Guild, snapshot: dict[str, Any], reason: str) -> RestoreReport:
    """Full restore: roles, channels, overwrites, settings and the icon."""
    report = RestoreReport(guild.id)
    if not snapshot:
        report.skip("no snapshot was available")
        return report
    if not _can_restore(guild):
        report.fail("I lack Administrator, so most of this cannot be undone")
        return report
    await restore_roles(guild, snapshot, report, reason)
    await restore_channels(guild, snapshot, report, reason)
    await restore_settings(guild, snapshot, report, snapshot.get("settings", {}).get("name") or reason)
    await restore_icon(guild, snapshot, report, reason)
    return report


def _can_restore(guild: discord.Guild) -> bool:
    perms = guild.me.guild_permissions if guild.me else None
    return bool(perms and perms.administrator)


def describe(snapshot: dict[str, Any]) -> str:
    """One-line summary for the snapshot list."""
    from datetime import datetime, timezone

    stamp = snapshot.get("created_at")
    when = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%d %b %Y %H:%M") if stamp else "unknown"
    return (
        f"`{stamp:.0f}` · {snapshot.get('label', 'automatic')} · {when} UTC · "
        f"{len(snapshot.get('roles', []))} roles · {len(snapshot.get('channels', []))} channels · "
        f"{len(snapshot.get('emojis', []))} emojis · {len(snapshot.get('stickers', []))} stickers"
    )