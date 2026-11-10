"""Pure helpers for choosing healthy music search sources.

This module intentionally has no Discord/Wavelink dependencies so its source
ordering and URL classification can be covered by standard-library tests.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

AUDIO_SEARCH_SOURCES = ("ytmsearch", "ytsearch", "scsearch", "bcsearch")
_SPOTIFY_URI_RE = re.compile(r"^spotify:(?:track|album|playlist):[A-Za-z0-9]+$", re.I)


def is_spotify_link(value: str) -> bool:
    """Return True for Spotify track/album/playlist URLs and Spotify URIs."""
    raw = (value or "").strip().strip("<>")
    if _SPOTIFY_URI_RE.fullmatch(raw):
        return True
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "spotify.link":
        return True
    if host not in {"spotify.com", "www.spotify.com", "open.spotify.com"}:
        return False
    path = re.sub(r"^/intl-[a-z]{2}(?:-[a-z]{2})?", "", parsed.path, flags=re.I)
    return bool(re.match(r"^/(?:track|album|playlist)/[A-Za-z0-9]+(?:/|$)", path, re.I))


def spotify_open_url(value: str) -> str:
    """Convert a Spotify URI to its official HTTPS URL for clickable Discord output."""
    raw = (value or "").strip().strip("<>")
    match = _SPOTIFY_URI_RE.fullmatch(raw)
    if match:
        _, item_type, item_id = raw.split(":", 2)
        return f"https://open.spotify.com/{item_type.lower()}/{item_id}"
    return raw


def normalize_audio_source(value: str | None, default: str) -> str:
    """Validate a configured source; never accept Spotify mirroring as an audio source."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in AUDIO_SEARCH_SOURCES else default


def ordered_search_sources(preferred: str, fallback: str, state=None, *, now: float | None = None) -> list[str]:
    """Build de-duplicated source order while respecting per-guild cooldowns."""
    preferred = normalize_audio_source(preferred, "ytmsearch")
    fallback = normalize_audio_source(fallback, "scsearch")
    order = list(dict.fromkeys([preferred, fallback, *AUDIO_SEARCH_SOURCES]))
    if state is None:
        return order
    import time
    current = time.monotonic() if now is None else now
    blocked = getattr(state, "blocked_sources", {})
    for source, expires_at in list(blocked.items()):
        if current >= expires_at:
            blocked.pop(source, None)
    return [source for source in order if source not in blocked]


def record_source_success(state, source: str) -> None:
    """A successful response clears that source's previous failure streak/cooldown."""
    if state is None:
        return
    getattr(state, "source_failures", {}).pop(source, None)
    getattr(state, "blocked_sources", {}).pop(source, None)


def record_source_failure(state, source: str, *, rate_limited: bool = False, now: float | None = None) -> int:
    """Record one source error and apply a temporary local cooldown."""
    if state is None:
        return 0
    import time
    current = time.monotonic() if now is None else now
    failures = getattr(state, "source_failures", None)
    blocked = getattr(state, "blocked_sources", None)
    if failures is None or blocked is None:
        return 0
    count = int(failures.get(source, 0)) + 1
    failures[source] = count
    if rate_limited or count >= 3:
        blocked[source] = current + (600.0 if rate_limited else 300.0)
    return count
