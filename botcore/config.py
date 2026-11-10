import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _env_id(name: str) -> int:
    value = os.getenv(name, "0").strip() or "0"
    try:
        return int(value)
    except ValueError as error:
        raise SystemExit(f"{name} must be a numeric Discord ID or 0.") from error


def _env_ids(name: str) -> tuple[int, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return ()

    ids: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item.isascii() or not item.isdigit() or int(item) == 0:
            raise SystemExit(f"{name} must be a comma-separated list of numeric Discord IDs.")
        user_id = int(item)
        if user_id not in ids:
            ids.append(user_id)
    return tuple(ids)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be true or false.")


def _env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip() or str(default)
    try:
        parsed = int(value)
    except ValueError as error:
        raise SystemExit(f"{name} must be a positive integer.") from error
    if parsed < 1:
        raise SystemExit(f"{name} must be a positive integer.")
    return parsed


def _env_domains(name: str) -> frozenset[str]:
    return frozenset(
        item.strip().lower().strip(".")
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


def _env_nonnegative_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip() or str(default)
    try:
        parsed = int(value)
    except ValueError as error:
        raise SystemExit(f"{name} must be a non-negative integer.") from error
    if parsed < 0:
        raise SystemExit(f"{name} must be a non-negative integer.")
    return parsed


def _lavalink_uri(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("LAVALINK_URI must be an http(s) URL or hostname.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_path: Path
    review_channel_id: int
    accepted_role_id: int
    staff_role_id: int
    log_channel_id: int
    trusted_role_management_bot_ids: tuple[int, ...]
    global_protected_owner_ids: tuple[int, ...]
    owner_hub_guild_id: int
    owner_hub_log_channel_id: int
    owner_hub_dashboard_channel_id: int
    lavalink_uri: str
    lavalink_password: str
    music_enabled: bool
    music_search_source: str
    premium_nickname_base: str
    premium_tick_emoji: str
    mass_mention_whitelist_role_ids: tuple[int, ...]
    mass_mention_decay_hours: int
    mass_mention_live_animation: bool
    link_filter_whitelisted_role_ids: tuple[int, ...]
    link_filter_whitelisted_channel_ids: tuple[int, ...]
    link_filter_whitelisted_domains: frozenset[str]
    link_filter_delete_delay: int
    link_filter_log_channel_id: int
    promo_image_warning_delete_delay: int

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        database_path = Path(os.getenv("DATABASE_PATH", "data.sqlite3")).expanduser()
        if not database_path.is_absolute():
            volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
            base_path = Path(volume_path).expanduser() if volume_path else root
            database_path = base_path / database_path

        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            database_path=database_path,
            review_channel_id=_env_id("CARMEET_REVIEW_CHANNEL_ID"),
            accepted_role_id=_env_id("CARMEET_ACCEPTED_ROLE_ID"),
            staff_role_id=_env_id("CARMEET_STAFF_ROLE_ID"),
            log_channel_id=_env_id("LOG_CHANNEL_ID"),
            trusted_role_management_bot_ids=_env_ids("TRUSTED_ROLE_MANAGEMENT_BOT_IDS"),
            global_protected_owner_ids=_env_ids("GLOBAL_PROTECTED_OWNER_IDS"),
            owner_hub_guild_id=_env_id("OWNER_HUB_GUILD_ID"),
            owner_hub_log_channel_id=_env_id("OWNER_HUB_LOG_CHANNEL_ID"),
            owner_hub_dashboard_channel_id=_env_id("OWNER_HUB_DASHBOARD_CHANNEL_ID"),
            lavalink_uri=_lavalink_uri(os.getenv("LAVALINK_URI", "")),
            lavalink_password=os.getenv("LAVALINK_PASSWORD", "").strip(),
            music_enabled=_env_bool("MUSIC_ENABLED", True),
            music_search_source=os.getenv("MUSIC_SEARCH_SOURCE", "scsearch").strip() or "scsearch",
            premium_nickname_base=os.getenv("PREMIUM_NICKNAME_BASE", "KYRO").strip() or "KYRO",
            premium_tick_emoji=os.getenv("PREMIUM_TICK_EMOJI", "✅").strip() or "✅",
            mass_mention_whitelist_role_ids=_env_ids("MASS_MENTION_WHITELIST_ROLE_IDS"),
            mass_mention_decay_hours=_env_positive_int("MASS_MENTION_DECAY_HOURS", 24),
            mass_mention_live_animation=_env_bool("LIVE_ANIMATION", False),
            link_filter_whitelisted_role_ids=_env_ids("LINK_FILTER_WHITELIST_ROLE_IDS"),
            link_filter_whitelisted_channel_ids=_env_ids("LINK_FILTER_WHITELIST_CHANNEL_IDS"),
            link_filter_whitelisted_domains=_env_domains("LINK_FILTER_WHITELIST_DOMAINS"),
            link_filter_delete_delay=_env_nonnegative_int("LINK_FILTER_DELETE_DELAY", 8),
            link_filter_log_channel_id=_env_id("LINK_FILTER_LOG_CHANNEL_ID"),
            promo_image_warning_delete_delay=_env_nonnegative_int("PROMO_IMAGE_WARNING_DELETE_DELAY", 8),
        )