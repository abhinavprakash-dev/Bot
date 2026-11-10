from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _bucket(state: dict[str, Any], guild_id: int, user_id: int) -> dict[str, Any]:
    warnings = state.setdefault("mass_mention_warnings", {})
    guild_bucket = warnings.setdefault(str(guild_id), {})
    return guild_bucket.setdefault(str(user_id), {"count": 0, "last_warning": None})


def get_warnings(
    state: dict[str, Any],
    guild_id: int,
    user_id: int,
    decay_hours: int = 24,
) -> int:
    record = _bucket(state, guild_id, user_id)
    last_warning = record.get("last_warning")
    if isinstance(last_warning, str):
        try:
            expired = datetime.now(timezone.utc) - datetime.fromisoformat(last_warning) > timedelta(hours=decay_hours)
        except ValueError:
            expired = True
        if expired:
            record["count"] = 0
    return max(0, min(4, int(record.get("count", 0))))


def add_warning(
    state: dict[str, Any],
    guild_id: int,
    user_id: int,
    decay_hours: int = 24,
) -> int:
    level = get_warnings(state, guild_id, user_id, decay_hours) + 1
    record = _bucket(state, guild_id, user_id)
    record["count"] = level
    record["last_warning"] = datetime.now(timezone.utc).isoformat()
    return min(level, 4)


def reset_warnings(state: dict[str, Any], guild_id: int, user_id: int) -> None:
    warnings = state.get("mass_mention_warnings", {})
    guild_bucket = warnings.get(str(guild_id))
    if isinstance(guild_bucket, dict):
        guild_bucket.pop(str(user_id), None)
