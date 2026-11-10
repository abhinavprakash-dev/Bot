"""Protection registry and audit-entry classification.

Everything in this module is pure logic: no network calls, no Discord state.
`cogs/security.py` owns the single audit listener and asks this module what a
given `AuditLogEntry` means before it decides whether to punish or revert.

The registry deliberately keeps the fifteen original protections byte-identical
in behaviour and only *adds* the new ones, so existing configs keep working.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import discord

A = discord.AuditLogAction

# --------------------------------------------------------------------- severity
LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
CRITICAL = "CRITICAL"
SEVERITY_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}
SEVERITY_COLOR = {
    LOW: 0x5865F2,
    MEDIUM: 0xF1C40F,
    HIGH: 0xE67E22,
    CRITICAL: 0xE74C3C,
}


@dataclass(frozen=True)
class Protection:
    """One toggleable protection."""

    key: str
    label: str
    description: str
    punishment: str
    category: str = "nuke"  # nuke | chat | permission | asset | structure | invite
    #: punish on the very first attempt instead of waiting for the threshold
    instant: bool = False
    #: punish even when the actor is whitelisted for this protection
    instant_unless_trusted: bool = True
    severity: str = MEDIUM


def _p(key, label, description, punishment, **kwargs) -> tuple[str, Protection]:
    return key, Protection(key, label, description, punishment, **kwargs)


# --------------------------------------------------------------------- registry
# `punishment` is the default; admins can still override it per protection in
# the existing PunishmentPanel.
PROTECTIONS: dict[str, Protection] = dict(
    [
        # ---- original twelve, unchanged behaviour -----------------------
        _p("antichannel", "Anti Channel", "Blocks unauthorised channel create, delete and update actions.", "ban"),
        _p("antirole", "Anti Role", "Blocks unauthorised role create, delete and update actions.", "ban"),
        _p("antiban", "Anti Ban/Kick", "Stops mass ban and mass kick attempts.", "ban"),
        _p("antibot", "Anti Bot Add", "Removes bots added by anyone who is not whitelisted.", "ban",
           instant=True, severity=CRITICAL),
        _p("antiwebhook", "Anti Webhook", "Blocks unauthorised webhook creation.", "ban", instant=True),
        _p("antiserver", "Anti Server Update",
           "Detects unauthorised server name, icon and settings changes and restores them.", "kick", instant=True),
        _p("antiintegration", "Anti Integration",
           "Detects unauthorised integration changes and deletes new integrations.", "ban", instant=True),
        _p("antimention", "Anti Mass Mention",
           "Deletes @everyone / @here spam and punishes repeat offenders.", "ban", category="chat"),
        _p("antipromo", "Anti Promotion",
           "Deletes server invites and removes casino or payout-style game promotions detected by local OCR.", "timeout",
           category="chat"),
        _p("antilink", "Anti Link", "Deletes messages containing links.", "ban", category="chat"),
        _p("anticaps", "Anti Caps", "Deletes messages written mostly in capital letters.", "ban", category="chat"),
        _p("antispam", "Spam Filter", "Stops message flooding and duplicate spam.", "timeout", category="chat"),
        _p("antiraid", "Anti Raid", "Detects join floods and removes the raiders.", "kick"),
        _p("antialt", "Anti Alt", "Detects and removes recently-created alt accounts.", "kick"),
        # ---- 13-32, the new layer --------------------------------------
        _p("antipermission", "Anti Permission Abuse",
           "Blocks unauthorised grants of dangerous permissions such as Administrator or Manage Server.", "ban",
           category="permission", instant=True, severity=CRITICAL),
        _p("antiadmingrant", "Anti Admin Grant",
           "Reverts an unauthorised Administrator grant and punishes whoever made it.", "ban",
           category="permission", instant=True, severity=CRITICAL),
        _p("antirolepermedit", "Anti Role Permission Edit",
           "Blocks unauthorised role permission changes and restores the previous state.", "ban",
           category="permission", instant=True, severity=HIGH),
        _p("antirolehoist", "Anti Role Hoist",
           "Blocks unauthorised role display (hoist) changes.", "kick", category="permission", instant=True),
        _p("antichannelpermedit", "Anti Channel Permission Edit",
           "Blocks unauthorised channel permission overwrites and restores the previous state.", "ban",
           category="permission", instant=True, severity=CRITICAL),
        _p("antichannelrename", "Anti Channel Rename",
           "Blocks mass or unauthorised channel renames.", "ban", category="structure"),
        _p("antirolerename", "Anti Role Rename",
           "Blocks mass or unauthorised role renames.", "ban", category="structure"),
        _p("antiemojidelete", "Anti Emoji Delete", "Restores emojis deleted by someone who is not trusted.", "ban",
           category="asset", instant=True, severity=HIGH),
        _p("antiemojicreate", "Anti Emoji Create", "Blocks mass emoji creation.", "ban", category="asset"),
        _p("antistickerdelete", "Anti Sticker Delete",
           "Restores stickers deleted by someone who is not trusted.", "ban", category="asset", instant=True,
           severity=HIGH),
        _p("antistickercreate", "Anti Sticker Create", "Blocks mass sticker creation.", "ban", category="asset"),
        _p("antivanity", "Anti Vanity URL Change",
           "Protects the server vanity URL. Discord does not allow it to be changed back by a bot.", "ban",
           category="structure", instant=True, severity=HIGH),
        _p("antiicon", "Anti Server Icon Change",
           "Restores the server icon after an unauthorised change.", "kick", category="structure", instant=True),
        _p("antiname", "Anti Server Name Change",
           "Restores the server name after an unauthorised change.", "kick", category="structure", instant=True),
        _p("anticommunity", "Anti Community Settings",
           "Protects onboarding and community feature configuration.", "ban", category="structure", instant=True,
           severity=HIGH),
        _p("antiautomod", "Anti AutoMod Update",
           "Blocks unauthorised AutoMod rule creation, deletion or disabling.", "ban", category="permission",
           instant=True, severity=CRITICAL),
        _p("antiinvitecreate", "Anti Invite Create", "Blocks mass invite creation. One invite is always allowed.",
           "ban", category="invite"),
        _p("antiinvitedelete", "Anti Invite Delete", "Blocks mass invite deletion.", "ban", category="invite"),
        _p("antisoundboard", "Anti Soundboard Abuse", "Blocks unauthorised soundboard changes and mass abuse.", "ban",
           category="asset"),
        _p("antistage", "Anti Stage/Forum Abuse",
           "Protects stage channels, forums and threads from unauthorised modification.", "ban",
           category="structure"),
    ]
)

NUKE_CATEGORY = "nuke"
CHAT_CATEGORY = "chat"

#: Protections that make up the "is anti-nuke on?" master flag.
NUKE_MODULES = tuple(
    key for key, value in PROTECTIONS.items() if value.category in (NUKE_CATEGORY, "permission", "structure", "asset", "invite")
)
#: Protections that make up the "is automod on?" master flag.
CHAT_MODULES = tuple(key for key, value in PROTECTIONS.items() if value.category == CHAT_CATEGORY)
#: Only these can be given a Ban / Kick / Timeout punishment.
PUNISHABLE_MODULES = tuple(
    key for key, value in PROTECTIONS.items()
    if key not in ("antilink", "anticaps")
)
PUNISHMENTS = {"ban": "Ban", "kick": "Kick", "timeout": "Timeout"}
MIN_TIMEOUT_MINUTES = 1
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
DEFAULT_TIMEOUT_MINUTES = MAX_TIMEOUT_MINUTES
DEFAULT_SPAM_TIMEOUT_MINUTES = 10

#: Protections that existed before the upgrade. Their commands, embeds and
#: config keys are untouched so no server notices a change.
LEGACY_MODULES = (
    "antichannel", "antirole", "antiban", "antibot", "antiwebhook", "antiserver",
    "antiintegration", "antimention", "antipromo", "antilink", "anticaps", "antispam",
    "antiraid", "antialt",
)
NEW_MODULES = tuple(key for key in PROTECTIONS if key not in LEGACY_MODULES)

# --------------------------------------------------------------- audit routing
# Audit actions that map onto exactly one protection regardless of the payload.
SIMPLE_ACTIONS: dict[Any, str] = {
    A.role_create: "antirole",
    A.role_delete: "antirole",
    A.ban: "antiban",
    A.kick: "antiban",
    A.member_prune: "antiban",
    A.bot_add: "antibot",
    A.webhook_create: "antiwebhook",
    A.integration_create: "antiintegration",
    A.integration_update: "antiintegration",
    A.integration_delete: "antiintegration",
    A.channel_create: "antichannel",
    A.channel_delete: "antichannel",
    A.emoji_delete: "antiemojidelete",
    A.emoji_create: "antiemojicreate",
    A.emoji_update: "antiemojicreate",
    A.sticker_delete: "antistickerdelete",
    A.sticker_create: "antistickercreate",
    A.sticker_update: "antistickercreate",
    A.invite_create: "antiinvitecreate",
    A.invite_delete: "antiinvitedelete",
    A.invite_update: "antiinvitecreate",
    A.soundboard_sound_create: "antisoundboard",
    A.soundboard_sound_delete: "antisoundboard",
    A.soundboard_sound_update: "antisoundboard",
    A.stage_instance_create: "antistage",
    A.stage_instance_update: "antistage",
    A.stage_instance_delete: "antistage",
    A.thread_create: "antistage",
    A.thread_update: "antistage",
    A.thread_delete: "antistage",
    A.automod_rule_create: "antiautomod",
    A.automod_rule_delete: "antiautomod",
    A.onboarding_create: "anticommunity",
    A.onboarding_update: "anticommunity",
    A.onboarding_prompt_create: "anticommunity",
    A.onboarding_prompt_update: "anticommunity",
    A.onboarding_prompt_delete: "anticommunity",
}
# NOTE: `member_role_update` is deliberately absent. It records *every*
# role change on a member (boosts, bot roles, integrations, onboarding
# role-selection) and Discord attributes system-driven changes to the
# member themself, so it gets its own classifier:
# `classify_member_role_update`.
# NOTE: the three automod actions are deliberately absent. They all point at the
# same protection, but only `_classify_automod_update` can tell a *disabled*
# rule (a pre-attack step) from an ordinary edit, so they get their own path.

#: `role_update` carries one action but up to six different protections, so it
#: is classified by looking at which keys actually changed.
ROLE_UPDATE_KEYS = {
    "permissions": "antirolepermedit",
    "hoist": "antirolehoist",
    "name": "antirolerename",
    "position": "antirolepermedit",
    "mentionable": "anticommunity",
}

#: `channel_update` likewise.
CHANNEL_UPDATE_KEYS = {
    "name": "antichannelrename",
    "permission_overwrites": "antichannelpermedit",
    "type": "antistage",
    "parent_id": "antistage",
    "flags": "antistage",
}

#: `overwrite_*` is the clean signal for channel permission changes.
OVERWRITE_ACTIONS = {A.overwrite_create, A.overwrite_update, A.overwrite_delete}

#: `guild_update` splits by which setting moved.
GUILD_UPDATE_KEYS = {
    "name": "antiname",
    "icon": "antiicon",
    "banner": "antiicon",
    "splash": "antiicon",
    "vanity_url_code": "antivanity",
}

# ------------------------------------------------------- permission sensitivity
#: Permissions that hand over real control of the server. Granting any of these
#: without authorisation is treated as a serious incident.
DANGEROUS_PERMISSIONS: dict[str, str] = {
    "administrator": "Administrator",
    "manage_guild": "Manage Server",
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_webhooks": "Manage Webhooks",
    "ban_members": "Ban Members",
    "kick_members": "Kick Members",
    "moderate_members": "Moderate Members",
    "manage_messages": "Manage Messages",
    "manage_expressions": "Manage Emojis, Stickers and Sounds",
    "manage_events": "Manage Events",
    "manage_threads": "Manage Threads",
    "manage_nicknames": "Manage Nicknames",
    "mention_everyone": "Mention Everyone",
    "move_members": "Move Members",
    "mute_members": "Mute Members",
}

#: Guild-level permissions only; making sense on a channel overwrite would
#: create false positives.
GUILD_ONLY_PERMISSIONS = frozenset(
    {
        "administrator", "manage_guild", "ban_members", "kick_members",
        "manage_webhooks", "manage_expressions", "view_audit_log", "manage_emojis_and_stickers",
        "create_expressions", "manage_events",
    }
)

#: Taking these away from @everyone is how an attacker locks staff out of their
#: own server. None of them are "dangerous" in themselves, which is why they
#: need their own list.
LOCKOUT_PERMISSIONS: dict[str, str] = {
    "send_messages": "Send Messages",
    "read_messages": "Read Messages",
    "add_reactions": "Add Reactions",
    "connect": "Connect",
    "speak": "Speak",
    "create_invite": "Create Invite",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
    "change_nickname": "Change Nickname",
}


def permissions_from_value(value: Any) -> Optional[int]:
    """Audit logs store permissions as an int, sometimes as a string."""
    if isinstance(value, discord.Permissions):
        return value.value
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def permission_delta(before: Any, after: Any) -> list[str]:
    """Names of permissions that were *granted* between two states."""
    old = permissions_from_value(before)
    new = permissions_from_value(after)
    if old is None or new is None:
        return []
    gained = discord.Permissions(new & ~old)
    names = []
    for attribute, label in DANGEROUS_PERMISSIONS.items():
        flag = getattr(discord.Permissions, attribute, None)
        if flag is None:
            continue
        if getattr(gained, attribute, False):
            names.append(label)
    return names


def permission_removed(before: Any, after: Any) -> list[str]:
    """Names of permissions that were *removed* between two states."""
    old = permissions_from_value(before)
    new = permissions_from_value(after)
    if old is None or new is None:
        return []
    lost = discord.Permissions(old & ~new)
    names = []
    for attribute, label in DANGEROUS_PERMISSIONS.items():
        if getattr(lost, attribute, False):
            names.append(label)
    return names


def overwrite_gained(before: Any, after: Any) -> list[str]:
    """Dangerous permissions granted by a channel overwrite change."""
    allow_old, deny_old = _split_overwrite(before)
    allow_new, deny_new = _split_overwrite(after)
    if allow_new is None or deny_new is None:
        return []
    gained = discord.Permissions(allow_new & ~allow_old)
    names = []
    for attribute, label in DANGEROUS_PERMISSIONS.items():
        if attribute in GUILD_ONLY_PERMISSIONS:
            continue
        if getattr(gained, attribute, False):
            names.append(label)
    return names


def overwrite_removed(before: Any, after: Any) -> list[str]:
    """Permissions a channel overwrite change took away (used to spot lockouts)."""
    allow_old, deny_old = _split_overwrite(before)
    allow_new, deny_new = _split_overwrite(after)
    if allow_old is None or deny_old is None or allow_new is None or deny_new is None:
        return []
    # allow_new & deny_old  ->  denied in the old state and now allowed
    now_forbidden = discord.Permissions(deny_new & ~deny_old)
    lost = discord.Permissions(allow_old & ~allow_new) | now_forbidden
    names = []
    for attribute, label in DANGEROUS_PERMISSIONS.items():
        if attribute in GUILD_ONLY_PERMISSIONS:
            continue
        if getattr(lost, attribute, False):
            names.append(label)
    return names


def overwrite_lockout(before: Any, after: Any) -> list[str]:
    """Permissions a change newly denies, i.e. what it takes away from people.

    Covers both directions Discord can use to remove access: clearing an `allow`
    bit, and setting a `deny` bit.
    """
    allow_old, deny_old = _split_overwrite(before)
    allow_new, deny_new = _split_overwrite(after)
    if allow_new is None or deny_new is None or allow_old is None or deny_old is None:
        return []
    lost = discord.Permissions(allow_old & ~allow_new) | discord.Permissions(deny_new & ~deny_old)
    names = []
    for attribute, label in LOCKOUT_PERMISSIONS.items():
        if getattr(lost, attribute, False):
            names.append(label)
    return names


def _split_overwrite(value: Any) -> tuple[Optional[int], Optional[int]]:
    if isinstance(value, discord.PermissionOverwrite):
        return value.pair()
    # Already-split pair. `_overwrite_pair` hands these back, so without this
    # branch every channel_update comparison would silently return "no change"
    # and the whole permission protection would be dead code.
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, (int, str, bool)) or item is None for item in value)
    ):
        return (permissions_from_value(value[0]) or 0, permissions_from_value(value[1]) or 0)
    if isinstance(value, dict):
        allow = permissions_from_value(value.get("allow", 0))
        deny = permissions_from_value(value.get("deny", 0))
        return (0 if allow is None else allow, 0 if deny is None else deny)
    if isinstance(value, (list, tuple)):
        allow, deny = 0, 0
        for item in value:
            if not isinstance(item, dict):
                continue
            got_allow = permissions_from_value(item.get("allow"))
            got_deny = permissions_from_value(item.get("deny"))
            if got_allow:
                allow |= got_allow
            if got_deny:
                deny |= got_deny
        return allow, deny
    return None, None


def is_everyone_overwrite(entry) -> bool:
    """True when the overwrite being changed applies to @everyone (id 0)."""
    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None)
    if target_id is None:
        return False
    if target_id == 0:
        return True
    return isinstance(target, discord.Role) and target.is_default()


# ------------------------------------------------------ member role grants
#: Granting a role with any permission in the shared danger registry is an
#: instant incident. Removing authority is not an escalation.
ROLE_DANGEROUS_PERMISSIONS: tuple[str, ...] = tuple(DANGEROUS_PERMISSIONS)


def role_is_ignorable(role: Any) -> bool:
    """True for @everyone and every role Discord manages itself.

    Booster, bot and integration roles are assigned by systems rather
    than by a user decision, so they must never count as a role-grant
    attack.
    """
    if role is None:
        return True
    if getattr(role, "id", None) in (None, 0):
        return True
    for method in ("is_default", "is_bot_managed", "is_premium_subscriber", "is_integration"):
        check = getattr(role, method, None)
        if callable(check) and check():
            return True
    return bool(getattr(role, "managed", False))


def member_role_change(entry) -> tuple[list[Any], list[Any]]:
    """(added, removed) roles recorded by a member_role_update entry.

    Discord stores the full role list before and after the change; the
    difference between the two is what actually happened. The raw lists
    can contain bare objects for roles that are not cached.
    """
    before = dict(getattr(entry, "before", None) or {})
    after = dict(getattr(entry, "after", None) or {})
    old_roles = [role for role in (before.get("roles") or []) if role is not None]
    new_roles = [role for role in (after.get("roles") or []) if role is not None]
    old_ids = {getattr(role, "id", None) for role in old_roles}
    new_ids = {getattr(role, "id", None) for role in new_roles}
    added = [role for role in new_roles if getattr(role, "id", None) not in old_ids]
    removed = [role for role in old_roles if getattr(role, "id", None) not in new_ids]
    return added, removed


def role_dangerous_flags(role: Any, dangerous_keys: Optional[Iterable[str]] = None) -> list[str]:
    """Labels of the dangerous permissions a role carries right now.

    Re-read on every event, so a role that gains dangerous permissions
    later is treated as dangerous from that moment on.
    """
    keys = tuple(dangerous_keys) if dangerous_keys else ROLE_DANGEROUS_PERMISSIONS
    value = permissions_from_value(getattr(role, "permissions", None))
    if value is None:
        return []
    labels: list[str] = []
    for key in keys:
        flag = getattr(discord.Permissions, key, None)
        if flag is None:
            continue
        bit = int(getattr(flag, "flag", 0) or 0)
        if bit and value & bit:
            labels.append(DANGEROUS_PERMISSIONS.get(key, key.replace("_", " ").title()))
    return labels


def integration_driven(entry) -> bool:
    """True when Discord attributes the change to an integration.

    Server boosts and bot integrations produce member_role_update
    entries whose `extra` carries an integration type; the recorded
    actor is the system, not a user choosing roles.
    """
    return bool(getattr(getattr(entry, "extra", None), "integration_type", None))


def classify_member_role_update(
    entry,
    *,
    added: Optional[list[Any]] = None,
    removed: Optional[list[Any]] = None,
    dangerous_keys: Optional[Iterable[str]] = None,
    self_assignable_roles: Optional[Iterable[int]] = None,
    ignore_onboarding_roles: bool = True,
) -> Optional[Detection]:
    """Classify a member_role_update entry (a role grant or revocation).

    `member_role_update` records *every* role change on a member,
    including boosts, bot roles and onboarding role-selection, and may
    attribute system-driven changes to the member themself. The entry
    is only an instant incident when it grants power:

        * dangerous permissions granted                     -> instant CRITICAL
        * non-dangerous self-role selection                 -> non-instant
        * other non-dangerous changes                       -> graduated
            (the caller can allow verified self-role and trusted-bot flows)
    """
    if added is None or removed is None:
        added, removed = member_role_change(entry)
    added = [role for role in (added or []) if not role_is_ignorable(role)]
    removed = [role for role in (removed or []) if not role_is_ignorable(role)]
    if not added and not removed:
        return None

    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None)
    executor_id = entry.user_id
    target_name = getattr(target, "name", None) or str(target_id)

    granted = [(role, role_dangerous_flags(role, dangerous_keys)) for role in added]
    dangerous = [(role, flags) for role, flags in granted if flags]

    details = {
        "audit_action": "member_role_update",
        "member_id": target_id,
        "executor_id": executor_id,
        "added_role_ids": [getattr(role, "id", 0) for role in added],
        "removed_role_ids": [getattr(role, "id", 0) for role in removed],
        "added_roles": [_role_display(role) for role in added],
        "removed_roles": [_role_display(role) for role in removed],
    }

    if dangerous:
        names = [_role_display(role) for role, _flags in dangerous]
        permissions = sorted({label for _role, flags in dangerous for label in flags})
        direction = []
        if any(flags for _role, flags in granted):
            direction.append("granted " + ", ".join(
                _role_display(role) for role, flags in granted if flags
            ))
        return Detection(
            module="antirolepermedit",
            severity=CRITICAL,
            reason=f"Dangerous role change on **{target_name}**: " + "; ".join(direction)
            + f" ({', '.join(permissions)}).",
            instant=True,
            escalation=True,
            details={**details, "dangerous_permissions": permissions, "dangerous_roles": names},
        )

    non_dangerous_removed = [
        role for role in removed if not role_dangerous_flags(role, dangerous_keys)
    ]
    if not added and not non_dangerous_removed:
        return None

    added_names = [_role_display(role) for role in added]
    removed_names = [_role_display(role) for role in removed]
    self_assign = (
        executor_id is not None
        and target_id is not None
        and int(executor_id) == int(target_id)
    )

    if self_assign and ignore_onboarding_roles:
        allowed = {int(role_id) for role_id in (self_assignable_roles or ())}
        changed_ids = {getattr(role, "id", 0) for role in added + removed}
        if allowed and changed_ids.issubset(allowed):
            # Onboarding / role-selection handing the member their game
            # roles: the member themself is recorded as the actor, which
            # is legitimate. Permissions were re-checked above on every
            # event, so a game role that gained dangerous permissions is
            # no longer ignored (it is classified as dangerous).
            return None
        unlisted = [
            _role_display(role) for role in added + removed
            if not allowed or getattr(role, "id", 0) not in allowed
        ]
        return Detection(
            module="antirolepermedit",
            severity=LOW,
            reason=f"**{target_name}** changed non-dangerous role(s): " + ", ".join(unlisted),
            instant=False,
            escalation=False,
            details={**details, "self_assign": True, "self_assignable": sorted(allowed)},
        )

    return Detection(
        module="antirolepermedit",
        severity=LOW,
        reason=f"Roles changed for **{target_name}**: added "
        + (", ".join(added_names) or "none")
        + ", removed "
        + (", ".join(removed_names) or "none")
        + " (no dangerous permissions).",
        instant=False,
        escalation=False,
        details={**details, "self_assign": self_assign},
    )


def _role_display(role: Any) -> str:
    name = getattr(role, "name", None)
    return str(name) if name else str(getattr(role, "id", "?"))


# ------------------------------------------------------------ rename heuristics
#: Channel/role names that a raid tries to hide behind.
SUSPICIOUS_NAME_TOKENS = frozenset(
    {
        "raid", "raided", "owned", "pwned", "hacked", "nuke", "nuked",
        "troll", "trolled", "piss", "shit", "fuck", "ass", "bitch",
        "cunt", "bastard", "dick", "bastards", "wtf", "stupid", "idiot",
    }
)

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def normalise_name(value: Optional[str]) -> str:
    return _NORMALISE_RE.sub(" ", (value or "").lower()).strip()


def name_is_offensive(name: Optional[str]) -> bool:
    words = set(normalise_name(name).split())
    return bool(words & SUSPICIOUS_NAME_TOKENS)


# ------------------------------------------------------------------ the result
@dataclass
class Detection:
    """What a single audit entry means for the protection system."""

    module: str
    severity: str
    reason: str
    instant: bool
    #: True when the action itself hands over control (a pre-attack step).
    escalation: bool = False
    #: Free-form values for the security log.
    details: dict[str, Any] = field(default_factory=dict)
    #: Audit action names worth reporting even when nothing is punished.
    notes: list[str] = field(default_factory=list)
    #: Per-detection action limit. None falls back to the protection's
    #: threshold; the graduated member role-grant path sets this so a
    #: non-dangerous change escalates instead of punishing instantly.
    limit: Optional[int] = None
    #: Per-detection sliding window in seconds. None uses the guild's
    #: security window.
    window: Optional[float] = None


def _protection(module: str) -> Protection:
    return PROTECTIONS[module]


def classify(entry) -> Optional[Detection]:
    """Map one audit entry onto a protection, or None when it is harmless.

    Pure and side-effect free so it can be unit tested with fake entries.
    """
    action = entry.action

    module = SIMPLE_ACTIONS.get(action)
    if module is not None:
        protection = _protection(module)
        return Detection(
            module=module,
            severity=protection.severity,
            reason=f"{action.name.replace('_', ' ')} is not allowed here ({protection.label}).",
            instant=protection.instant,
            details={"audit_action": action.name},
        )

    if action in OVERWRITE_ACTIONS:
        return _classify_overwrite(entry)

    if action == A.role_update:
        return _classify_role_update(entry)

    if action == A.channel_update:
        return _classify_channel_update(entry)

    if action == A.guild_update:
        return _classify_guild_update(entry)

    if action == A.automod_rule_update:
        return _classify_automod_update(entry)

    return None


def _classify_overwrite(entry) -> Optional[Detection]:
    """Permission overwrite change: granting power or locking people out.

    Narrowing an overwrite (removing an allow) is deliberately *not* reported:
    it makes the server stricter and is never an attack.
    """
    changes = getattr(entry, "changes", None) or {}
    before, after = None, None
    for key in ("permission_overwrites", "permissions"):
        if key in changes and isinstance(changes.get(key), dict):
            before = changes[key].get("old")
            after = changes[key].get("new")
    everyone = is_everyone_overwrite(entry)

    if action_is_delete(entry.action):
        return Detection(
            module="antichannelpermedit",
            severity=CRITICAL if everyone else HIGH,
            reason="A channel permission overwrite was deleted"
            + (" for @everyone." if everyone else "."),
            instant=True,
            escalation=True,
            details={"audit_action": entry.action.name, "everyone": everyone},
        )

    gained = overwrite_gained(before, after)
    lost = overwrite_lockout(before, after)

    if gained:
        return Detection(
            module="antichannelpermedit",
            severity=CRITICAL if everyone else HIGH,
            reason=("@everyone was granted " if everyone else "The overwrite was granted ")
            + ", ".join(gained)
            + ".",
            instant=True,
            escalation=True,
            details={
                "audit_action": entry.action.name,
                "everyone": everyone,
                "gained": gained,
                "removed": lost,
            },
        )
    if lost:
        return Detection(
            module="antichannelpermedit",
            severity=HIGH if everyone else MEDIUM,
            reason="A channel overwrite denied " + ", ".join(lost)
            + (" for @everyone." if everyone else "."),
            instant=True,
            escalation=everyone,
            details={"audit_action": entry.action.name, "everyone": everyone, "removed": lost},
        )
    return None


def action_is_delete(action: Any) -> bool:
    return action in (A.overwrite_delete, A.role_delete, A.channel_delete, A.sticker_delete, A.emoji_delete)


def _classify_role_update(entry) -> Optional[Detection]:
    changes = getattr(entry, "changes", None) or {}
    if not changes:
        return None
    target = getattr(entry, "target", None)
    target_name = getattr(target, "name", "a role")

    if "permissions" in changes:
        before = changes["permissions"].get("old")
        after = changes["permissions"].get("new")
        gained = permission_delta(before, after)
        removed = permission_removed(before, after)
        if gained:
            admin = "Administrator" in gained
            return Detection(
                module="antiadmingrant" if admin else "antirolepermedit",
                severity=CRITICAL if admin else HIGH,
                reason=f"**{target_name}** was granted " + ", ".join(gained) + ".",
                instant=True,
                escalation=True,
                details={
                    "audit_action": "role_update",
                    "role_id": getattr(target, "id", None),
                    "role_name": target_name,
                    "gained": gained,
                    "removed": removed,
                },
            )
        if removed:
            return Detection(
                module="antirolepermedit",
                severity=MEDIUM,
                reason=f"Permissions were removed from **{target_name}**: " + ", ".join(removed) + ".",
                instant=True,
                details={
                    "audit_action": "role_update",
                    "role_id": getattr(target, "id", None),
                    "role_name": target_name,
                    "removed": removed,
                },
            )

    if "hoist" in changes:
        new_hoist = changes["hoist"].get("new")
        old_hoist = changes["hoist"].get("old")
        return Detection(
            module="antirolehoist",
            severity=MEDIUM if new_hoist else LOW,
            reason=f"**{target_name}** was {'hoisted so it displays separately' if new_hoist else 'un-hoisted'}"
            + (f" (it was {'hoisted' if old_hoist else 'not hoisted'} before)." if old_hoist is not None else "."),
            instant=True,
            details={
                "audit_action": "role_update",
                "role_id": getattr(target, "id", None),
                "role_name": target_name,
                "hoist_old": old_hoist,
                "hoist_new": new_hoist,
            },
        )

    if "name" in changes:
        return _rename_detection(
            "antirolerename",
            getattr(target, "id", None),
            changes["name"].get("old"),
            changes["name"].get("new"),
            f"role **{target_name}**",
        )

    if "position" in changes:
        return Detection(
            module="antirolepermedit",
            severity=MEDIUM,
            reason=f"**{target_name}** was moved in the role hierarchy "
            f"({changes['position'].get('old')} → {changes['position'].get('new')}).",
            instant=True,
            escalation=True,
            details={"audit_action": "role_update", "role_id": getattr(target, "id", None),
                     "role_name": target_name},
        )

    if "mentionable" in changes:
        return Detection(
            module="anticommunity",
            severity=LOW,
            reason=f"**{target_name}** mentionability was changed.",
            instant=True,
            details={"audit_action": "role_update", "role_id": getattr(target, "id", None),
                     "role_name": target_name},
        )
    return None


def _rename_detection(module: str, target_id, old_name, new_name, what: str) -> Detection:
    notes: list[str] = []
    severity = MEDIUM
    if name_is_offensive(new_name):
        severity = HIGH
        notes.append("the new name contains abusive words")
    elif old_name and normalise_name(old_name) != normalise_name(new_name or ""):
        severity = MEDIUM
    return Detection(
        module=module,
        severity=severity,
        reason=f"{what} was renamed from **{old_name}** to **{new_name}**.",
        instant=False,
        details={"audit_action": "update", "target_id": target_id, "old_name": old_name, "new_name": new_name},
        notes=notes,
    )


def _classify_channel_update(entry) -> Optional[Detection]:
    changes = getattr(entry, "changes", None) or {}
    if not changes:
        return None
    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None)
    channel_name = getattr(target, "name", "a channel")

    if "permission_overwrites" in changes:
        change = changes["permission_overwrites"]
        before, after = change.get("old"), change.get("new")
        gained = overwrite_gained(_overwrite_pair(before, 0), _overwrite_pair(after, 0))
        # A lockout can go either way, so check both directions.
        lost = overwrite_lockout(before, after) or overwrite_removed(before, after)
        details = {
            "audit_action": "channel_update",
            "channel_id": target_id,
            "channel_name": channel_name,
            "gained": gained,
            "removed": lost,
        }
        if gained:
            return Detection(
                module="antichannelpermedit",
                severity=CRITICAL if "Manage Roles" in gained or "Manage Messages" in gained else HIGH,
                reason=f"@everyone was granted {', '.join(gained)} in #{channel_name}.",
                instant=True,
                escalation=True,
                details=details,
            )
        if lost:
            return Detection(
                module="antichannelpermedit",
                severity=HIGH,
                reason=f"#{channel_name} lost {', '.join(lost)} for @everyone, which can lock staff out.",
                instant=True,
                escalation=True,
                details=details,
            )
        # A non-@everyone overwrite change still matters, but report it without
        # claiming a specific permission was gained.
        return Detection(
            module="antichannelpermedit",
            severity=MEDIUM,
            reason=f"Permission overwrites in #{channel_name} were changed.",
            instant=True,
            details=details,
        )

    if "name" in changes:
        return _rename_detection(
            "antichannelrename",
            target_id,
            changes["name"].get("old"),
            changes["name"].get("new"),
            f"channel **#{channel_name}**",
        )

    if "type" in changes or "parent_id" in changes or "flags" in changes:
        return Detection(
            module="antistage",
            severity=MEDIUM,
            reason=f"#{channel_name} structure changed (type, category or flags).",
            instant=True,
            details={"audit_action": "channel_update", "channel_id": target_id, "channel_name": channel_name},
        )
    return None


def _overwrite_map(value: Any) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            allow = permissions_from_value(item.get("allow", 0)) or 0
            deny = permissions_from_value(item.get("deny", 0)) or 0
            result[item_id] = (allow, deny)
    return result


def _overwrite_pair(value: Any, target_id: int) -> tuple[int, int]:
    pair = _overwrite_map(value).get(target_id)
    return pair if pair is not None else (0, 0)


def _classify_guild_update(entry) -> Optional[Detection]:
    changes = getattr(entry, "changes", None) or {}
    if not changes:
        return None
    detected: list[tuple[str, str, str]] = []  # (module, label, severity)
    for key, module in GUILD_UPDATE_KEYS.items():
        if key in changes:
            detected.append((module, key, PROTECTIONS[module].severity))
    if not detected:
        return None
    module = detected[0][0]
    labels = ", ".join(label for _m, label, _s in detected)
    severity = max((s for _m, _l, s in detected), key=lambda value: SEVERITY_ORDER[value])
    instant = any(_protection(m).instant for m, _l, _s in detected)
    return Detection(
        module=module,
        severity=severity,
        reason=f"Server settings changed: {labels}.",
        instant=instant,
        escalation=module in ("antiadmingrant", "antiname"),
        details={
            "audit_action": "guild_update",
            "keys": [label for _m, label, _s in detected],
            "raw": {label: changes.get(label) for _m, label, _s in detected},
        },
    )


def _classify_automod_update(entry) -> Optional[Detection]:
    changes = getattr(entry, "changes", None) or {}
    target = getattr(entry, "target", None)
    rule_id = getattr(target, "id", None)
    rule_name = getattr(target, "name", "an AutoMod rule")
    disabled = False
    if "enabled" in changes and changes["enabled"].get("new") is False:
        disabled = True
    if entry.action == A.automod_rule_delete:
        return Detection(
            module="antiautomod",
            severity=CRITICAL,
            reason=f"The AutoMod rule **{rule_name}** was deleted.",
            instant=True,
            escalation=True,
            details={"audit_action": "automod_rule_delete", "rule_id": rule_id, "rule_name": rule_name},
        )
    if entry.action == A.automod_rule_create:
        return Detection(
            module="antiautomod",
            severity=HIGH,
            reason=f"A new AutoMod rule **{rule_name}** was created.",
            instant=True,
            details={"audit_action": "automod_rule_create", "rule_id": rule_id, "rule_name": rule_name},
        )
    return Detection(
        module="antiautomod",
        severity=CRITICAL if disabled else HIGH,
        reason=f"The AutoMod rule **{rule_name}** was {'disabled' if disabled else 'changed'}.",
        instant=True,
        escalation=disabled,
        details={
            "audit_action": "automod_rule_update",
            "rule_id": rule_id,
            "rule_name": rule_name,
            "disabled": disabled,
        },
    )


def module_label(module: str) -> str:
    protection = PROTECTIONS.get(module)
    return protection.label if protection else module


def instant_modules() -> Iterable[str]:
    return (key for key, value in PROTECTIONS.items() if value.instant)