"""Emergency lockdown.

When a coordinated attack is detected the server needs to stop changing while a
human decides what to do. Discord gives a bot exactly two levers:

* deny permissions on a channel (`@everyone` overwrite)
* time out or kick accounts

What it cannot do is revoke a role that was already granted, so a lockdown
never pretends to undo privilege escalation. It reports what it actually did.

Every operation is permission-checked and every failure is recorded, because a
lockdown that silently half-works is worse than one that says so.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import discord


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

#: Default duration. Long enough to finish handling a raid, short enough that a
#: forgotten lockdown does not disable a server for a week.
DEFAULT_DURATION = 900
MAX_DURATION = 86400
MIN_DURATION = 60

#: Permissions denied to @everyone while locked. Members can still read
#: channels, while chat, thread creation, reactions, and server-management
#: actions are blocked.
#:
#: This is a list rather than a `Permissions` literal because a deny overwrite
#: that included `administrator` would silently blank the whole bitfield, and
#: because a name that `Permissions()` does not accept raises at import time,
#: which would take the entire bot down instead of one feature.
LOCKDOWN_DENY_NAMES = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "add_reactions",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "moderate_members",
    "manage_messages",
    "manage_expressions",
    "manage_events",
    "manage_threads",
    "move_members",
    "mute_members",
    "mention_everyone",
    "create_expressions",
)


def _build_deny() -> discord.Permissions:
    """OR together every named permission that this discord.py version knows.

    Permission names live on the class as `flag_value` descriptors, which are
    not ints. `.flag` holds the raw bit. Permissions this version does not know
    are skipped instead of raising, so a discord.py upgrade cannot break the
    import and take the whole bot down.
    """
    value = 0
    for name in set(LOCKDOWN_DENY_NAMES):
        flag = getattr(discord.Permissions, name, None)
        raw = getattr(flag, "flag", None)
        if isinstance(raw, int):
            value |= raw
    return discord.Permissions(value)


LOCKDOWN_DENY = _build_deny()

#: Channels that must stay usable during a lockdown: logs, staff channels and
#: anything explicitly configured as protected.
DEFAULT_SAFE_NAMES = ("mod", "staff", "admin", "log", "security", "audit")


@dataclass
class LockdownState:
    """What a lockdown is doing right now, per guild."""

    guild_id: int
    active: bool = False
    until: float = 0.0
    reason: str = ""
    triggered_by: str = ""
    #: (channel_id, allow, deny) so the exact overwrite can be put back
    touched: list[tuple[int, int, int]] = field(default_factory=list)
    locked_members: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Injected by the manager so `remaining()` uses the same clock the timer was
    #: started with. Comparing against wall time instead would report a live
    #: lockdown as finished whenever the two clocks disagree.
    clock: Callable[[], float] = time.time

    def remaining(self, now: Optional[float] = None) -> int:
        if not self.active:
            return 0
        return max(0, int(self.until - (now if now is not None else self.clock())))

    def expired(self) -> bool:
        return self.active and self.until <= self.clock()

    def summary(self) -> str:
        if not self.active:
            return "Lockdown is **not** active."
        return f"Lockdown is **ON** for {self.remaining() // 60} more minute(s)."


@dataclass
class LockdownResult:
    """Outcome of engaging or releasing a lockdown."""

    engaged: bool
    state: LockdownState
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[str]:
        return [note for note in self.notes if note.startswith("could not")]


class LockdownManager:
    """Owns lockdown state per guild and performs the permission changes."""

    def __init__(
        self,
        *,
        state_for: Callable[[int], dict],
        save: Callable[[], None],
        clock: Callable[[], float] = time.time,
        notify: Optional[Callable[[discord.Guild, str], Any]] = None,
    ) -> None:
        self._state_for = state_for
        self._save = save
        self.clock = clock
        self._notify = notify
        self.live: dict[int, LockdownState] = {}

    # ------------------------------------------------------------ persistence
    def _persisted(self, guild_id: int) -> dict:
        store = self._state_for(guild_id).setdefault("lockdowns", {})
        return store.setdefault(str(guild_id), {"active": False, "until": 0.0, "touched": [], "locked_members": [], "notes": []})

    def restore_on_startup(self, guild: discord.Guild) -> Optional[LockdownState]:
        """Reload state after a restart.

        A lockdown must survive a crash: otherwise restarting the bot would be a
        free way for an attacker to reset the server's defences. Expired state
        is kept until the sweeper can restore the channel overwrites.
        """
        stored = self._persisted(guild.id)
        if not stored.get("active"):
            return None
        state = LockdownState(
            guild_id=guild.id,
            active=True,
            until=float(stored.get("until", 0)),
            reason=stored.get("reason", ""),
            triggered_by=stored.get("triggered_by", ""),
            touched=[tuple(item) for item in stored.get("touched", [])],
            locked_members=list(stored.get("locked_members", [])),
            notes=list(stored.get("notes", [])),
            clock=self.clock,
        )
        self.live[guild.id] = state
        return state

    def _persist(self, state: LockdownState) -> None:
        self._state_for(state.guild_id).setdefault("lockdowns", {})[str(state.guild_id)] = {
            "active": state.active,
            "until": state.until,
            "reason": state.reason,
            "triggered_by": state.triggered_by,
            "touched": [list(item) for item in state.touched],
            "locked_members": list(state.locked_members),
            "notes": state.notes[-20:],
        }
        self._save()

    # ----------------------------------------------------------------- state
    def is_active(self, guild_id: int) -> bool:
        state = self.live.get(guild_id)
        if state is None or not state.active:
            return False
        return state.until > self.clock()

    def state_of(self, guild_id: int) -> Optional[LockdownState]:
        return self.live.get(guild_id)

    def forget(self, guild_id: int) -> None:
        self.live.pop(guild_id, None)

    # --------------------------------------------------------------- engage
    async def engage(
        self,
        guild: discord.Guild,
        *,
        reason: str = "",
        triggered_by: str = "",
        duration: int = DEFAULT_DURATION,
        channel_ids: Optional[Iterable[int]] = None,
        lock_members: Optional[Iterable[int]] = None,
    ) -> LockdownResult:
        """Deny dangerous permissions to @everyone until `duration` runs out."""
        duration = max(MIN_DURATION, min(MAX_DURATION, int(duration)))
        state = self.live.get(guild.id)
        if state is None or not state.active:
            state = LockdownState(guild_id=guild.id, clock=self.clock)
        state.active = True
        state.until = self.clock() + duration
        state.reason = reason or "No reason given"
        state.triggered_by = triggered_by or "Automatic detection"
        self.live[guild.id] = state
        notes: list[str] = []
        touched_ids = {channel_id for channel_id, _, _ in state.touched}

        if not (guild.me and guild.me.guild_permissions.administrator):
            # Without Administrator the bot can still deny per-channel
            # permissions, so try that instead of doing nothing at all.
            notes.append("could not read full permissions for this server; only channel locks were applied")

        targets = list(channel_ids) if channel_ids is not None else self._default_channels(guild)
        if not targets:
            notes.append("could not find any channel to lock")

        for channel_id in targets:
            channel = guild.get_channel(channel_id)
            if channel is None:
                notes.append(f"could not find channel `{channel_id}`")
                continue
            try:
                existing = channel.overwrites_for(guild.default_role)
                allow, deny = existing.pair()
                allow_value = int(getattr(allow, "value", allow) or 0)
                deny_value = int(getattr(deny, "value", deny) or 0)
                if channel.id not in touched_ids:
                    state.touched.append((channel.id, allow_value, deny_value))
                    touched_ids.add(channel.id)
                await channel.set_permissions(
                    guild.default_role,
                    overwrite=discord.PermissionOverwrite.from_pair(
                        discord.Permissions(allow_value),
                        discord.Permissions(deny_value | LOCKDOWN_DENY.value),
                    ),
                    reason="Security lockdown",
                )
            except discord.Forbidden:
                notes.append(f"could not lock #{channel.name}")
            except (discord.HTTPException, AttributeError, TypeError):
                notes.append(f"could not lock #{channel.name}")

        for user_id in lock_members or []:
            member = guild.get_member(user_id)
            if member is None:
                continue
            try:
                await member.timeout(
                    utcnow() + timedelta(seconds=duration),
                    reason="Security lockdown",
                )
                state.locked_members.append(user_id)
            except discord.Forbidden:
                notes.append(f"could not time out `{user_id}`")
            except (discord.HTTPException, TypeError, ValueError):
                notes.append(f"could not time out `{user_id}`")

        if not notes:
            notes.append(f"locked {len(state.touched)} channel(s)")
        state.notes = notes
        self._persist(state)
        return LockdownResult(engaged=True, state=state, notes=notes)

    async def release(self, guild: discord.Guild) -> LockdownResult:
        """Put every overwrite the lockdown touched back exactly as it was."""
        state = self.live.get(guild.id)
        if state is None or not state.active:
            return LockdownResult(
            engaged=False,
            state=LockdownState(guild_id=guild.id, clock=self.clock),
            notes=["Lockdown was not active"],
        )

        notes: list[str] = []
        failed_touched: list[tuple[int, int, int]] = []
        for channel_id, allow, deny in state.touched:
            channel = guild.get_channel(channel_id)
            if channel is None:
                notes.append(f"could not find channel `{channel_id}` to unlock")
                continue
            try:
                await channel.set_permissions(
                    guild.default_role,
                    overwrite=discord.PermissionOverwrite.from_pair(
                        discord.Permissions(allow), discord.Permissions(deny)
                    ),
                    reason="Security lockdown lifted",
                )
            except discord.Forbidden:
                notes.append(f"could not unlock #{channel.name}")
                failed_touched.append((channel_id, allow, deny))
            except (discord.HTTPException, AttributeError, TypeError):
                notes.append(f"could not unlock #{channel.name}")
                failed_touched.append((channel_id, allow, deny))

        failed_members: list[int] = []
        for user_id in state.locked_members:
            member = guild.get_member(user_id)
            if member is None:
                continue
            try:
                await member.timeout(None, reason="Security lockdown lifted")
            except discord.Forbidden:
                notes.append(f"could not remove the timeout from `{user_id}`")
                failed_members.append(user_id)
            except (discord.HTTPException, TypeError, ValueError):
                notes.append(f"could not remove the timeout from `{user_id}`")
                failed_members.append(user_id)

        state.active = bool(failed_touched or failed_members)
        if not state.active:
            state.until = 0.0
        state.touched = failed_touched
        state.locked_members = failed_members
        state.notes = notes or ["lockdown lifted"]
        self._persist(state)
        return LockdownResult(engaged=False, state=state, notes=state.notes)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _default_channels(guild: discord.Guild) -> list[int]:
        """Lock everything except a few channels that must stay usable.

        Leaving the log channel open is the difference between an admin being
        able to see the lockdown and being able to undo it.
        """
        keep_open: set[int] = set()
        for channel in guild.channels:
            lowered = channel.name.lower()
            if any(token in lowered for token in DEFAULT_SAFE_NAMES):
                keep_open.add(channel.id)
        return [
            channel.id
            for channel in guild.channels
            if channel.id not in keep_open
        ]

    @staticmethod
    def safe_channel_ids(guild: discord.Guild) -> list[int]:
        """Channels that stay open during a lockdown, so staff can still act."""
        result = []
        for channel in guild.channels:
            lowered = channel.name.lower()
            if any(token in lowered for token in DEFAULT_SAFE_NAMES):
                result.append(channel.id)
        return sorted(set(result))


async def sweep_expired(manager: LockdownManager, guild: discord.Guild) -> Optional[LockdownResult]:
    """Release a lockdown whose timer ran out. Safe to call repeatedly."""
    state = manager.state_of(guild.id)
    if state is None or not state.active:
        return None
    if state.until > manager.clock():
        return None
    return await manager.release(guild)