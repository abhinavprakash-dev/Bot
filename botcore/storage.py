import asyncio
import json
import os
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


DEFAULT_STATE = {
    "next_id": 1,
    "event": {},
    "tournament": {},
    "carmeet_applications": {},
    "settings": {},
    "subscriptions": {},
    "warnings": {},
    "mass_mention_warnings": {},
}


def _migrate_database_to_volume(database_path: Path) -> None:
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    configured_path = Path(os.getenv("DATABASE_PATH", "data.sqlite3")).expanduser()
    if not volume_path or configured_path.is_absolute() or database_path.exists():
        return

    legacy_path = Path(__file__).resolve().parents[1] / configured_path
    if not legacy_path.is_file() or legacy_path.resolve() == database_path.resolve():
        return

    temporary_path = database_path.with_name(database_path.name + ".migration.tmp")
    source = sqlite3.connect(legacy_path)
    try:
        destination = sqlite3.connect(temporary_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
        os.replace(temporary_path, database_path)
    finally:
        source.close()
        if temporary_path.exists():
            temporary_path.unlink()


class SQLiteStateStore:
    """Persist the bot's existing state dictionary as one atomic SQLite record."""

    def __init__(self, database_path: Path, legacy_json_path: Path) -> None:
        self.database_path = database_path
        self.legacy_json_path = legacy_json_path
        self._lock = RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        _migrate_database_to_volume(self.database_path)
        self._connection = sqlite3.connect(self.database_path, timeout=10, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS bot_state (state_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._connection.commit()

    def load_state(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM bot_state WHERE state_key = ?", ("main",)
            ).fetchone()
            if row is not None:
                state = json.loads(row[0])
            elif self.legacy_json_path.exists():
                with self.legacy_json_path.open("r", encoding="utf-8") as legacy_file:
                    state = json.load(legacy_file)
            else:
                state = {}

            for key, default_value in DEFAULT_STATE.items():
                state.setdefault(key, default_value.copy() if isinstance(default_value, dict) else default_value)
            self.save_state(state)
            return state

    def serialize(self, state: dict[str, Any]) -> str:
        """Encode the state on the calling thread so a later write cannot see a half-mutated dict."""
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    def write_payload(self, payload: str) -> None:
        with self._lock:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO bot_state (state_key, payload) VALUES (?, ?) "
                    "ON CONFLICT(state_key) DO UPDATE SET payload = excluded.payload",
                    ("main", payload),
                )

    def save_state(self, state: dict[str, Any]) -> None:
        self.write_payload(self.serialize(state))

    async def save_state_async(self, state: dict[str, Any]) -> None:
        """Run only the blocking SQLite commit off the event loop."""
        payload = self.serialize(state)
        await asyncio.to_thread(self.write_payload, payload)

    def close(self) -> None:
        with self._lock:
            self._connection.close()