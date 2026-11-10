from __future__ import annotations

import asyncio
import io
import math
import os
import random
import re
from urllib.parse import urlparse
import struct
import time
import unicodedata
import uuid
import zlib
from pathlib import Path
from collections import defaultdict, deque
from dataclasses import dataclass, field

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from botcore import ui
from botcore.music_sources import (
    is_spotify_link,
    normalize_audio_source,
    ordered_search_sources,
    record_source_failure,
    record_source_success,
    spotify_open_url,
)

# Load .env here too: this module reads its settings at import time, and if it is
# imported before main.py the variables below would silently fall back to their
# defaults. load_dotenv() is idempotent, so calling it again in main.py is fine.
load_dotenv()

# ---------------- Settings (can be set as environment variables) ----------------
# Optional direct link to your own waveform GIF. If empty, assets/waveform.gif is attached instead.
WAVEFORM_GIF_URL = os.getenv("MUSIC_PANEL_GIF", "").strip()
ASSET_DIR = Path(__file__).resolve().parent / "assets"
ICON_DIR = ASSET_DIR / "icons"
WAVEFORM_PATH = ASSET_DIR / "waveform.gif"
ICON_PREFIX = "kp_"  # name prefix of the application emojis the bot creates for the buttons
FALLBACK_ICONS = {
    "prev": "⏮️", "pause": "⏸️", "play": "▶️", "next": "⏭️", "stop": "⏹️",
    "loop": "🔁", "volup": "🔊", "voldown": "🔉", "forward": "⏩", "back": "⏪",
    "lyrics": "📝", "radio": "📻", "disconnect": "🚪",
    "shuffle": "🔀", "queue": "📜",
}
# Audio search providers only. Spotify URLs open in Spotify instead of being
# relayed into shared Discord voice playback.
SEARCH_SOURCE = normalize_audio_source(os.getenv("MUSIC_SEARCH_SOURCE", "ytmsearch"), "ytmsearch")
FALLBACK_SOURCE = normalize_audio_source(os.getenv("MUSIC_FALLBACK_SOURCE", "ytsearch"), "ytsearch")
if SEARCH_SOURCE in {"ytsearch", "ytmsearch"}:
    print(
        f"Music: configured source {SEARCH_SOURCE!r} uses YouTube-backed audio; "
        "confirm Lavalink YouTube OAuth is authorized."
    )
# How many times the same track may fail before it is skipped.
MAX_TRACK_ATTEMPTS = 10
MAX_QUEUE_TRACKS = 500  # max tracks taken from one playlist/album link
INACTIVE_SECONDS = 300  # leave voice after this long with nothing playing
PANEL_COLOR = 0x5865F2
PANEL_ACCENT = 0x8B7CF6  # purple bar on the left of the player card
PANEL_REFRESH_SECONDS = 20  # how often the progress bar is refreshed while playing
FILTER_PRESETS = {
    "normal": ("Normal", "No audio effects"),
    "bassboost": ("Bass boost", "Heavier low end"),
    "nightcore": ("Nightcore", "Faster and higher pitch"),
    "vaporwave": ("Vaporwave", "Slower and deeper"),
    "8d": ("8D audio", "Sound rotates around you"),
    "karaoke": ("Karaoke", "Reduces vocals"),
}
import re
URL_RE = re.compile(r"https?://", re.I)


def fmt_duration(ms: int) -> str:
    total = max(0, int(ms) // 1000)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def build_filters(name: str) -> "wavelink.Filters":
    filters = wavelink.Filters()
    if name == "bassboost":
        filters.equalizer.set(
            bands=[{"band": 0, "gain": 0.2}, {"band": 1, "gain": 0.15}, {"band": 2, "gain": 0.1}, {"band": 3, "gain": 0.05}]
        )
    elif name == "nightcore":
        filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
    elif name == "vaporwave":
        filters.timescale.set(speed=0.85, pitch=0.8, rate=1.0)
    elif name == "8d":
        filters.rotation.set(rotation_hz=0.2)
    elif name == "karaoke":
        filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
    return filters


FILTER_REQUIRES = {
    "bassboost": "equalizer",
    "nightcore": "timescale",
    "vaporwave": "timescale",
    "8d": "rotation",
    "karaoke": "karaoke",
}


def fmt_clock(ms: int) -> str:
    total = max(0, int(ms) // 1000)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def progress_bar(position: int, length: int, size: int = 10) -> str:
    ratio = 0.0 if length <= 0 else max(0.0, min(1.0, position / length))
    filled = int(ratio * size)
    return "▰" * filled + "▱" * (size - filled)


def _lzw(indices: bytes, min_size: int) -> bytes:
    """GIF flavoured LZW compression."""
    clear = 1 << min_size
    eoi = clear + 1
    code_size = min_size + 1
    out = bytearray()
    bitbuf = 0
    bitcnt = 0
    table: dict[int, int] = {}
    next_code = eoi + 1

    def emit(code: int, size: int) -> None:
        nonlocal bitbuf, bitcnt
        bitbuf |= code << bitcnt
        bitcnt += size
        while bitcnt >= 8:
            out.append(bitbuf & 255)
            bitbuf >>= 8
            bitcnt -= 8

    emit(clear, code_size)
    prefix = indices[0]
    for px in indices[1:]:
        key = (prefix << 8) | px
        code = table.get(key)
        if code is not None:
            prefix = code
            continue
        emit(prefix, code_size)
        if next_code < 4096:
            table[key] = next_code
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
            next_code += 1
        else:
            emit(clear, code_size)
            table.clear()
            next_code = eoi + 1
            code_size = min_size + 1
        prefix = px
    emit(prefix, code_size)
    emit(eoi, code_size)
    if bitcnt:
        out.append(bitbuf & 255)
    return bytes(out)


def make_waveform_gif(width: int = 820, height: int = 72, bars: int = 60, frames: int = 20, delay: int = 7) -> bytes:
    """Animated purple equaliser drawn with the standard library only (no Pillow, no asset file)."""
    rng = random.Random(11)
    bar_w = 8
    step = width / bars
    base = []
    level = 0.6
    for _ in range(bars):
        level = max(0.45, min(1.0, level + rng.choice((-0.2, -0.1, 0.1, 0.2))))
        base.append(level)
    speeds = [rng.choice((1, 1, 2, 3)) for _ in range(bars)]
    phases = [rng.random() * 6.2832 for _ in range(bars)]

    palette = bytearray(64 * 3)  # index 0 = transparent background, 1..bars = bar colours
    for i in range(bars):
        t = i / max(1, bars - 1)
        palette[(i + 1) * 3:(i + 1) * 3 + 3] = bytes((int(99 + 69 * t), int(102 - 17 * t), int(241 + 6 * t)))

    insets = []
    for dx in range(bar_w):
        dist = abs(dx + 0.5 - bar_w / 2)
        insets.append(int(round(bar_w / 2 - max(0.0, (bar_w / 2) ** 2 - dist * dist) ** 0.5)))

    gif = bytearray(b"GIF89a")
    gif += struct.pack("<HHBBB", width, height, 0xF5, 0, 0)
    gif += bytes(palette)
    gif += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"
    for f in range(frames):
        px = bytearray(width * height)
        for i in range(bars):
            wave = 0.5 + 0.5 * math.sin(6.2832 * speeds[i] * f / frames + phases[i])
            bar_h = max(6, int(base[i] * (0.18 + 0.82 * wave) * (height - 4)))
            top = (height - bar_h) // 2
            x0 = int(i * step + (step - bar_w) / 2)
            colour = i + 1
            for dx in range(bar_w):
                inset = insets[dx]
                for y in range(top + inset, top + bar_h - inset):
                    px[y * width + x0 + dx] = colour
        gif += b"\x21\xF9\x04" + bytes((0x09,)) + struct.pack("<H", delay) + b"\x00\x00"
        gif += b"\x2C" + struct.pack("<HHHHB", 0, 0, width, height, 0)
        gif += bytes((6,))
        data = _lzw(bytes(px), 6)
        for k in range(0, len(data), 255):
            block = data[k:k + 255]
            gif += bytes((len(block),)) + block
        gif += b"\x00"
    gif += b"\x3B"
    return bytes(gif)


_WAVE_GIF: bytes | None = None
_WAVE_GIF_FAILED = False


def wave_gif() -> bytes | None:
    global _WAVE_GIF, _WAVE_GIF_FAILED
    if _WAVE_GIF is None and not _WAVE_GIF_FAILED:
        try:
            _WAVE_GIF = make_waveform_gif()
        except Exception as error:
            _WAVE_GIF_FAILED = True
            print(f"[music] could not draw the waveform animation: {type(error).__name__}: {error}")
    return _WAVE_GIF


def queue_front(queue, track) -> None:
    queue.put_at(0, track)


def search_sources(preferred: str, state: MusicState | None = None) -> list[str]:
    """Preferred provider first, configured fallback second, then other safe sources."""
    return ordered_search_sources(preferred, FALLBACK_SOURCE, state)


# Boilerplate words that sit in the title of almost every upload. They must never
# decide which result wins, otherwise a reupload named "Song Name (Full Song)"
# would outrank the official track for the query "song name song".
FILLER_QUERY_TOKENS = {
    "song", "songs", "video", "videos", "audio", "music", "official", "full",
    "new", "latest", "hd", "hq", "mp3", "lyric", "lyrics", "the", "a", "an",
    "of", "and", "ft", "feat", "featuring",
}
# A disallowed version (cover, slowed, remix, ...) must be filtered out even for a
# long query, so the penalty has to dwarf any possible token match count.
RESULT_REJECTION_PENALTY = 1000


def ranked_search_results(tracks: list, query: str) -> list:
    if not tracks:
        return tracks
    query_tokens = set(re.findall(r"[^\W_]+", query.casefold()))
    if not query_tokens:
        return tracks
    # Drop boilerplate words from the query so they cannot boost a result.
    content_tokens = query_tokens - FILLER_QUERY_TOKENS
    if not content_tokens:
        content_tokens = set(query_tokens)
    content_query = " ".join(
        token
        for token in re.findall(r"[^\W_]+", query.casefold())
        if token in content_tokens
    )
    normalized_query = " ".join(re.findall(r"[^\W_]+", query.casefold()))

    version_markers = {
        "slowed", "reverb", "lofi", "remix", "cover", "live", "nightcore",
        "mashup", "karaoke", "instrumental", "acoustic", "unplugged", "sped",
        "speed", "intro", "outro", "edit", "teaser", "snippet", "preview",
        "trailer", "status", "reel", "short", "shorts", "whatsapp", "tiktok",
        "fanmade", "fan", "bootleg", "8d", "mp3", "reaction", "lyric", "lyrics",
        "hq", "hd", "compilation", "mix", "dance",
    }
    unwanted_markers = version_markers - query_tokens
    if "lo fi" in normalized_query:
        unwanted_markers.discard("lofi")
    requests_short_form = bool(
        query_tokens & {"intro", "teaser", "snippet", "preview", "trailer", "short", "shorts"}
    )

    def dominant_script(text: str) -> str | None:
        counts: dict[str, int] = {}
        for character in text:
            if not character.isalpha():
                continue
            name = unicodedata.name(character, "")
            script = name.split(" ", 1)[0] if name else ""
            if script:
                counts[script] = counts.get(script, 0) + 1
        return max(counts, key=counts.__getitem__) if counts else None

    query_script = dominant_script(query)

    def score(track, index: int) -> tuple[int, int]:
        title_words = re.findall(r"[^\W_]+", str(track.title).casefold())
        title_tokens = set(title_words)
        author_words = re.findall(r"[^\W_]+", str(track.author).casefold())
        author_tokens = set(author_words)
        matches = len(content_tokens & title_tokens) * 2 + len(content_tokens & author_tokens)
        title = " ".join(re.findall(r"[^\W_]+", str(track.title).casefold()))
        title_content = " ".join(
            token for token in title_words if token not in FILLER_QUERY_TOKENS
        )
        author_content = " ".join(
            token for token in author_words if token not in FILLER_QUERY_TOKENS
        )
        title_exact = title_content == content_query
        title_phrase_exact = title == normalized_query
        title_prefix = title_content.startswith(f"{content_query} ")
        author_exact = author_content == content_query
        extra_title_tokens = len(
            set(title_content.split()) - content_tokens
        )
        has_unwanted_version = bool(title_tokens & unwanted_markers)
        matched_tokens = content_tokens & (title_tokens | author_tokens)
        minimum_matches = max(1, (len(content_tokens) * 2 + 2) // 3)
        has_weak_match = len(matched_tokens) < minimum_matches
        title_script = dominant_script(title_content)
        has_script_mismatch = bool(
            query_script
            and query_script != "LATIN"
            and title_script == "LATIN"
        )
        length = getattr(track, "length", 0) or 0
        if 0 < length < 60_000 and not requests_short_form:
            has_unwanted_version = True
        if "lo fi" in title and "lo fi" not in normalized_query:
            has_unwanted_version = True
        if "bass boosted" in title and not {"bass", "boosted"} & query_tokens:
            has_unwanted_version = True
        # The artist's own upload ("Official", "... - Topic", "ArtistVEVO") breaks
        # ties in favour of the real track when several results match equally.
        official = (
            int("official" in title_tokens) * 4
            + int("topic" in author_tokens) * 3
            + int("vevo" in author_tokens) * 3
        )
        title_relevance = (
            int(title_exact) * 8
            + int(title_phrase_exact) * 4
            + int(title_prefix) * 4
            + int(author_exact) * 4
            - extra_title_tokens
        )
        return (
            matches + official + title_relevance
            - int(has_unwanted_version or has_weak_match or has_script_mismatch)
            * RESULT_REJECTION_PENALTY,
            -index,
        )

    ranked = sorted(enumerate(tracks), key=lambda item: score(item[1], item[0]), reverse=True)
    return [track for index, track in ranked if score(track, index)[0] >= 2]


def preferred_search_result(tracks: list, query: str):
    ranked = ranked_search_results(tracks, query)
    return ranked[0] if ranked else None


@dataclass
class MusicState:
    guild_id: int | None = None
    text_channel_id: int | None = None
    #: When true the bot must never leave this voice channel, even with nothing playing.
    stay_247: bool = False
    #: The channel the bot was told to stay in (may differ from the current one).
    stay_channel_id: int | None = None
    autoplay: bool = False
    post_now_playing: bool = True
    access_level: str = "everyone"
    dj_role_id: int | None = None
    panel_message: discord.Message | None = None
    panel_track_id: str | None = None
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = False
    loop: str = "off"  # off | track | queue
    radio: bool = False
    filter: str = "normal"
    current: wavelink.Playable | None = None
    prev: wavelink.Playable | None = None
    requesters: dict = field(default_factory=dict)
    search_queries: dict[str, str] = field(default_factory=dict)
    retried: set = field(default_factory=set)
    attempts: dict[str, int] = field(default_factory=dict)
    recent: deque = field(default_factory=lambda: deque(maxlen=30))
    fail_streak: int = 0
    started_at: float = 0.0
    paused_at: float | None = None
    paused_total: float = 0.0
    offset_ms: int = 0
    desync_ticks: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    blocked_sources: dict[str, float] = field(default_factory=dict)
    source_failures: dict[str, int] = field(default_factory=dict)


IN_PLACE_ACTIONS = {"pause", "loop", "volup", "voldown", "forward", "back", "radio", "shuffle"}


# ----------------------------------- The cog ------------------------------------
class MusicPanel(commands.Cog):
    music = app_commands.Group(
        name="music", description="Music playback and queue controls", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lavalink_uri = getattr(bot, "lavalink_uri", "")
        self.lavalink_password = getattr(bot, "lavalink_password", "")
        self.states: dict[int, MusicState] = {}
        #: guild_id -> source chosen with `.music source`. Lives outside MusicState
        #: because that object is dropped when the bot leaves voice, and a source
        #: preference must survive a disconnect.
        self.source_prefs: dict[int, str] = {}
        self.connect_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.autocomplete_tracks: dict[str, tuple[int, int, float, wavelink.Playable, str]] = {}
        self.icons: dict[str, str] = dict(FALLBACK_ICONS)
        self._connect_task: asyncio.Task | None = None
        self._stay_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # Loud, greppable line so it is obvious which code and source are live.
        print(
            f"Music: search source = {SEARCH_SOURCE!r}; "
            f"order = {', '.join(search_sources(SEARCH_SOURCE))}"
        )
        print("Music: player card v5 loaded (animated waveform).")
        if not WAVEFORM_GIF_URL and not WAVEFORM_PATH.exists():
            await asyncio.to_thread(wave_gif)
        await self.load_icons()
        self.restore_247_settings()
        self.bot.add_view(self.build_dispatch_view())
        self.watchdog.start()
        self.panel_ticker.start()
        self._stay_task = asyncio.create_task(self._stay_loop())
        if not self.lavalink_uri or not self.lavalink_password:
            print("Music: LAVALINK_URI or LAVALINK_PASSWORD is empty; music is disabled.")
            return
        self._connect_task = asyncio.create_task(self._connect_loop())

    async def cog_unload(self) -> None:
        self.watchdog.cancel()
        self.panel_ticker.cancel()
        if self._stay_task is not None:
            self._stay_task.cancel()
            try:
                await self._stay_task
            except asyncio.CancelledError:
                pass
        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        try:
            await wavelink.Pool.close()
        except Exception as error:
            print(f"Music: Lavalink shutdown failed ({type(error).__name__}: {error}).")

    async def _connect_loop(self) -> None:
        """Supervise the Lavalink connection for as long as the bot runs.

        On Railway the bot and the Lavalink node are separate services, so the
        node is usually not listening yet when the bot boots, and it restarts on
        its own during a deploy. The old code connected once and, on failure,
        returned while printing that wavelink would retry - nothing retried, so
        one unlucky start left music dead until the bot itself was restarted.
        This keeps trying with a backoff and says plainly what is wrong.
        """
        delay = 10.0
        reported = False
        warned_auth = False
        while not self.bot.is_closed():
            if self.node_ready():
                delay = 10.0
                if reported:
                    print(f"Music: Lavalink is reachable again at {self.lavalink_uri}.")
                    reported = False
                await asyncio.sleep(30)
                continue

            try:
                if "main" in wavelink.Pool.nodes:
                    # Registered by an earlier success but now down: re-handshake
                    # the existing node so its players and identifier survive.
                    await wavelink.Pool.reconnect()
                else:
                    await wavelink.Pool.connect(
                        client=self.bot,
                        nodes=[
                            wavelink.Node(
                                identifier="main",
                                uri=self.lavalink_uri,
                                password=self.lavalink_password,
                                retries=1,
                            )
                        ],
                    )
            except wavelink.exceptions.AuthorizationFailedException:
                if not warned_auth:
                    print(
                        f"Music: Lavalink rejected the password for {self.lavalink_uri}. "
                        "LAVALINK_PASSWORD must match the node's server password."
                    )
                    warned_auth = True
            except Exception as error:
                if not reported:
                    print(f"Music: cannot reach Lavalink yet ({type(error).__name__}: {error}).")

            if self.node_ready():
                continue

            if not reported:
                print(
                    f"Music: Lavalink is offline at {self.lavalink_uri}. "
                    f"Retrying every {int(delay)}s until it comes back."
                )
                reported = True
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 1.6)

    async def _stay_loop(self) -> None:
        """Keep the bot in every voice channel it is told to stay in 24/7.

        A guild that opted in with `.music247 on` expects the bot to be present
        even when nobody is playing anything, so the channel is not abandoned
        while the bot is asleep, restarted, or while a track is being searched.
        """
        while not self.bot.is_closed():
            try:
                await self._stay_pass()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[music] stay loop failed: {type(error).__name__}: {error}")
            await asyncio.sleep(30)

    async def _stay_pass(self) -> None:
        for guild_id, state in list(self.states.items()):
            if not state.stay_247:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            target_id = state.stay_channel_id or guild_id
            channel = guild.get_channel(target_id) if target_id else None
            if channel is None or not isinstance(channel, discord.VoiceChannel):
                continue
            player = guild.voice_client
            if isinstance(player, wavelink.Player):
                if player.channel.id == channel.id:
                    continue
                try:
                    await player.move_to(channel)
                except Exception as error:
                    print(f"[music] stay move failed guild={guild.id}: {type(error).__name__}: {error}")
                continue
            if not self.node_ready():
                continue
            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True)
                player.autoplay = (
                    wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.disabled
                )
                state.stay_channel_id = channel.id
            except Exception as error:
                print(f"[music] stay join failed guild={guild.id}: {type(error).__name__}: {error}")

    def state(self, guild_id: int) -> MusicState:
        state = self.states.setdefault(guild_id, MusicState())
        state.guild_id = guild_id
        return state

    def restore_247_settings(self) -> None:
        bot_state = getattr(self.bot, "state", None)
        saved = bot_state.get("music_247", {}) if isinstance(bot_state, dict) else {}
        preferences = bot_state.get("music_preferences", {}) if isinstance(bot_state, dict) else {}
        if isinstance(preferences, dict):
            for raw_guild_id, config in preferences.items():
                if not isinstance(config, dict):
                    continue
                try:
                    guild_id = int(raw_guild_id)
                except (TypeError, ValueError):
                    continue
                if guild_id > 0:
                    state = self.state(guild_id)
                    state.autoplay = config.get("autoplay") is True
                    state.post_now_playing = config.get("post_now_playing") is not False
                    access_level = config.get("access_level")
                    if isinstance(access_level, str) and access_level in {"everyone", "dj", "admin"}:
                        state.access_level = access_level
                    try:
                        state.text_channel_id = int(config.get("panel_channel_id") or 0) or None
                    except (TypeError, ValueError):
                        state.text_channel_id = None
                    try:
                        state.dj_role_id = int(config.get("dj_role_id") or 0) or None
                    except (TypeError, ValueError):
                        state.dj_role_id = None
        if not isinstance(saved, dict):
            return
        for raw_guild_id, config in saved.items():
            if not isinstance(config, dict):
                continue
            try:
                guild_id = int(raw_guild_id)
                channel_id = int(config.get("channel_id") or 0)
            except (TypeError, ValueError):
                continue
            if guild_id <= 0 or channel_id < 0:
                continue
            enabled = config.get("enabled") is True
            if not enabled and not channel_id:
                continue
            state = self.state(guild_id)
            state.stay_247 = enabled
            state.stay_channel_id = channel_id or None

    def persist_247_settings(self) -> None:
        bot_state = getattr(self.bot, "state", None)
        if not isinstance(bot_state, dict):
            raise RuntimeError("The bot state store is unavailable.")
        saved = {
            str(guild_id): {
                "enabled": state.stay_247,
                "channel_id": state.stay_channel_id,
            }
            for guild_id, state in self.states.items()
            if state.stay_247 or state.stay_channel_id is not None
        }
        bot_state["music_247"] = saved
        bot_state["music_preferences"] = {
            str(guild_id): {
                "autoplay": state.autoplay,
                "post_now_playing": state.post_now_playing,
                "access_level": state.access_level,
                "panel_channel_id": state.text_channel_id,
                "dj_role_id": state.dj_role_id,
            }
            for guild_id, state in self.states.items()
            if (
                state.autoplay
                or not state.post_now_playing
                or state.access_level != "everyone"
                or state.text_channel_id is not None
                or state.dj_role_id is not None
            )
        }
        save = getattr(self.bot, "save", None)
        if not callable(save):
            raise RuntimeError("The bot state save function is unavailable.")
        save()

    def preferred_source(self, guild_id: int | None) -> str:
        """The search source to try first for this guild.

        Reads the guild's own choice when one was set, otherwise the global
        default from the environment. Never depends on `globals()` mutation.
        """
        if guild_id is not None:
            chosen = self.source_prefs.get(guild_id)
            if chosen:
                return chosen
        return SEARCH_SOURCE

    # ------------------------------ helpers ------------------------------
    @staticmethod
    def node_ready() -> bool:
        try:
            node = wavelink.Pool.get_node()
        except Exception:
            return False
        return node.status == wavelink.NodeStatus.CONNECTED

    async def ensure_player(self, guild: discord.Guild, member: discord.Member):
        access_error = self.music_access_error(guild, member)
        if access_error:
            return None, access_error
        if not self.node_ready():
            return None, "Music server (Lavalink) is offline right now."
        state = self.state(guild.id)
        existing_player = guild.voice_client
        if isinstance(existing_player, wavelink.Player):
            existing_player.autoplay = (
                wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.disabled
            )
            try:
                await guild.change_voice_state(channel=existing_player.channel, self_deaf=True)
            except Exception:
                pass
            return existing_player, None
        stay_channel = guild.get_channel(state.stay_channel_id) if state.stay_channel_id else None
        if state.stay_247 and stay_channel is not None and isinstance(stay_channel, discord.VoiceChannel):
            # The guild keeps the bot in one channel 24/7. A member who is not
            # there is told to join it instead of being told to join voice at all.
            if member.voice is None or member.voice.channel is None:
                return None, f"Join {stay_channel.mention} to use music."
            if member.voice.channel != stay_channel:
                return None, f"Join {stay_channel.mention} to use music (the 24/7 channel)."
            channel = stay_channel
        else:
            if member.voice is None or member.voice.channel is None:
                return None, "Join a voice channel first."
            channel = member.voice.channel
        bot_member = guild.me
        state = self.states.get(guild.id)
        saved_channel = guild.get_channel(state.text_channel_id) if state and state.text_channel_id else None
        if isinstance(saved_channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return saved_channel
        if bot_member is not None:
            permissions = channel.permissions_for(bot_member)
            missing = [
                name
                for name in ("view_channel", "connect", "speak")
                if not getattr(permissions, name)
            ]
            if missing:
                labels = ", ".join(name.replace("_", " ").title() for name in missing)
                return None, f"I need the {labels} permission in {channel.mention} to join and play music."
        async with self.connect_locks[guild.id]:
            player = guild.voice_client
            if player is not None and not isinstance(player, wavelink.Player):
                return None, "The bot is already connected to voice for something else."
            if player is not None:
                if player.channel != channel:
                    return None, "The bot is already playing music in another voice channel."
                player.autoplay = (
                    wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.disabled
                )
                try:
                    await guild.change_voice_state(channel=player.channel, self_deaf=True)
                except Exception:
                    pass
                return player, None
            try:
                print(f"[music] voice join start guild={guild.id} channel={channel.id}")
                player = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=30)
                print(f"[music] voice join success guild={guild.id} channel={channel.id}")
            except asyncio.TimeoutError as error:
                print(f"[music] voice join timeout guild={guild.id} channel={channel.id}: {type(error).__name__}: {error}")
                return None, "Voice connection timed out. Check Lavalink and Discord voice connectivity."
            except discord.Forbidden as error:
                print(
                    f"[music] voice join forbidden guild={guild.id} channel={channel.id} "
                    f"code={error.code}"
                )
                return None, "Could not join the voice channel. Check the bot's Connect and Speak permissions."
            except discord.HTTPException as error:
                print(
                    f"[music] voice join failed guild={guild.id} channel={channel.id} "
                    f"status={error.status} code={error.code}"
                )
                return None, "Discord rejected the voice connection. Check the channel permissions and try again."
            except wavelink.WavelinkException as error:
                print(
                    f"[music] voice join failed guild={guild.id} channel={channel.id} "
                    f"error={type(error).__name__}: {error}"
                )
                return None, "Music voice connection failed. Check the Lavalink connection and try again."
            player.autoplay = (
                wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.disabled
            )
            player.inactive_timeout = INACTIVE_SECONDS
            bot_perms = channel.permissions_for(guild.me)
            if bot_perms.manage_channels and channel.bitrate < 96000:
                target = int(min(96000, guild.bitrate_limit * 1000))
                try:
                    await channel.edit(bitrate=target, reason="Music: raising bitrate for audio quality")
                except discord.HTTPException:
                    pass
            # A new voice connection must not discard the server's 24/7 preference.
            self.states[guild.id] = MusicState(
                guild_id=guild.id,
                stay_247=state.stay_247,
                stay_channel_id=state.stay_channel_id,
            )
            return player, None

    def control_player(
        self,
        guild: discord.Guild | None,
        member: discord.Member | discord.User,
    ) -> tuple[wavelink.Player | None, MusicState | None, str | None]:
        if guild is None or not isinstance(member, discord.Member):
            return None, None, "Use this inside a server."
        access_error = self.music_access_error(guild, member)
        if access_error:
            return None, None, access_error
        player = guild.voice_client
        if not isinstance(player, wavelink.Player):
            return None, None, "The bot is not in a voice channel."
        if member.voice is None or member.voice.channel != player.channel:
            return None, None, "Join the bot's voice channel to control playback."
        return player, self.state(guild.id), None

    def music_access_error(self, guild: discord.Guild, member: discord.Member) -> str | None:
        state = self.state(guild.id)
        if state.access_level == "everyone":
            return None
        permissions = member.guild_permissions
        if permissions.administrator or permissions.manage_guild:
            return None
        if state.access_level == "admin":
            return "Music is limited to server administrators."
        if state.access_level == "dj":
            if state.dj_role_id and any(role.id == state.dj_role_id for role in member.roles):
                return None
            return "Music is limited to the configured DJ role."
        return "Music access is not configured correctly."

    async def notify(self, state: MusicState, text: str) -> None:
        channel = self.bot.get_channel(state.text_channel_id) if state.text_channel_id else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await channel.send(text, delete_after=15)
        except discord.HTTPException:
            pass

    # ------------------------------ panel ------------------------------
    async def load_icons(self) -> None:
        """Upload assets/icons/*.png once as application emojis and use them on the buttons.
        Falls back to normal emojis if anything goes wrong."""
        try:
            existing = {emoji.name: emoji for emoji in await self.bot.fetch_application_emojis()}
        except Exception as error:
            print(f"[music] could not read application emojis ({type(error).__name__}); using default icons.")
            return
        for name in FALLBACK_ICONS:
            app_name = f"{ICON_PREFIX}{name}"
            emoji = existing.get(app_name)
            path = ICON_DIR / f"{name}.png"
            if emoji is None and path.exists():
                try:
                    emoji = await self.bot.create_application_emoji(name=app_name, image=path.read_bytes())
                except Exception as error:
                    print(f"[music] could not upload icon '{name}': {type(error).__name__}: {error}")
            if emoji is not None:
                self.icons[name] = str(emoji)

    def make_button(
        self,
        action: str,
        icon: str | None = None,
        active: bool = False,
        label: str | None = None,
        style: discord.ButtonStyle | None = None,
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            emoji=self.icons.get(icon or action),
            label=label,
            style=style or (discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary),
            custom_id=f"kryo:music_panel:{action}",
        )

        async def callback(interaction: discord.Interaction, _action: str = action) -> None:
            await self.handle_button(interaction, _action)

        button.callback = callback
        return button

    def make_filter_select(self, current: str = "normal") -> discord.ui.Select:
        if current not in FILTER_PRESETS:
            current = "normal"
        select = discord.ui.Select(
            custom_id="kryo:music_panel:filter",
            placeholder=f"Audio filter: {FILTER_PRESETS[current][0]}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, value=key, description=description)
                for key, (name, description) in FILTER_PRESETS.items()
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            value = select.values[0] if select.values else "normal"
            await self.handle_filter(interaction, value)

        select.callback = callback
        return select

    def build_dispatch_view(self) -> discord.ui.LayoutView:
        """Register stable component IDs so panel buttons continue working after restarts."""
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container()
        rows = (
            ("prev", "pause", "skip"),
            ("voldown", "volup", "loop"),
            ("radio", "back", "forward"),
            ("queue", "lyrics", "shuffle"),
            ("stop", "disconnect"),
        )
        for index, actions in enumerate(rows):
            row = discord.ui.ActionRow()
            for action in actions:
                row.add_item(self.make_button(action))
            container.add_item(row)
            if index == 3:
                select_row = discord.ui.ActionRow()
                select_row.add_item(self.make_filter_select())
                container.add_item(select_row)
        view.add_item(container)
        return view

    @staticmethod
    def panel_files() -> list[discord.File]:
        if WAVEFORM_GIF_URL:
            return []
        if WAVEFORM_PATH.exists():
            return [discord.File(WAVEFORM_PATH, filename="waveform.gif")]
        data = wave_gif()
        return [discord.File(io.BytesIO(data), filename="waveform.gif")] if data else []

    def build_view(self, player: wavelink.Player, state: MusicState) -> discord.ui.LayoutView:
        """Premium player card. Never raises: falls back to a plain card if anything goes wrong."""
        try:
            return self._build_view(player, state)
        except Exception as error:
            print(f"[music] could not build the player card: {type(error).__name__}: {error}")
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("**Music player**\nControls are loading, play a song again if this stays."),
                    accent_colour=discord.Colour(PANEL_ACCENT),
                )
            )
            return view

    def _build_view(self, player: wavelink.Player, state: MusicState) -> discord.ui.LayoutView:
        track = state.current
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour(PANEL_ACCENT))
        if track is None:
            container.add_item(discord.ui.TextDisplay("**Nothing is playing**"))
            view.add_item(container)
            return view

        safe_title = discord.utils.escape_markdown(track.title or "Unknown title").replace("[", "(").replace("]", ")")[:200]
        title_text = f"## [{safe_title}]({track.uri})" if track.uri else f"## {safe_title}"
        artist = discord.utils.escape_markdown(track.author or "Unknown artist")[:80]
        requester = state.requesters.get(track.identifier)
        details = f"{artist}\n-# Requested by <@{requester}>" if requester else artist
        status = "PAUSED" if player.paused else "NOW PLAYING"
        container.add_item(discord.ui.TextDisplay(f"-# {status}"))

        artwork = track.artwork if isinstance(track.artwork, str) and track.artwork.startswith("http") else None
        if artwork:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_text),
                    discord.ui.TextDisplay(details),
                    accessory=discord.ui.Thumbnail(artwork),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(title_text))
            container.add_item(discord.ui.TextDisplay(details))

        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

        if WAVEFORM_GIF_URL:
            wave_media = WAVEFORM_GIF_URL
        elif WAVEFORM_PATH.exists() or wave_gif():
            wave_media = "attachment://waveform.gif"
        else:
            wave_media = None
        if wave_media:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=wave_media)
            container.add_item(gallery)
        if track.is_stream or track.length <= 0:
            timeline = "`LIVE`"
        else:
            position = max(0, min(int(player.position), track.length))
            timeline = f"`{fmt_clock(position)}` {progress_bar(position, track.length)} `-{fmt_clock(track.length - position)}`"
        container.add_item(discord.ui.TextDisplay(timeline))

        def icon(name: str, fallback: str) -> str:
            return self.icons.get(name) or fallback

        loop_names = {"off": "Off", "track": "Track", "queue": "Queue"}
        container.add_item(
            discord.ui.TextDisplay(
                f"{icon('volup', '🔊')} Volume **{player.volume}%**   {icon('loop', '🔁')} Loop **{loop_names.get(state.loop, 'Off')}**\n"
                f"{icon('radio', '♾️')} Autoplay **{'On' if state.radio else 'Off'}**   {icon('queue', '📜')} Queue **{len(player.queue)}**"
            )
        )
        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

        active = discord.ButtonStyle.primary
        rows = [
            [
                self.make_button("prev", label="Prev"),
                self.make_button(
                    "pause",
                    icon="play" if player.paused else "pause",
                    label="Resume" if player.paused else "Pause",
                    style=active,
                ),
                self.make_button("skip", icon="next", label="Skip"),
            ],
            [
                self.make_button("voldown", label="Vol-"),
                self.make_button("volup", label="Vol+"),
                self.make_button("loop", label="Loop", style=active if state.loop != "off" else None),
            ],
            [
                self.make_button("radio", label="Auto", style=active if state.radio else None),
                self.make_button("back", label="-10s"),
                self.make_button("forward", label="+10s"),
            ],
            [
                self.make_button("queue", label="Queue"),
                self.make_button("lyrics", label="Lyrics"),
                self.make_button("shuffle", label="Shuffle"),
            ],
        ]
        for buttons in rows:
            row = discord.ui.ActionRow()
            for button in buttons:
                row.add_item(button)
            container.add_item(row)
        select_row = discord.ui.ActionRow()
        select_row.add_item(self.make_filter_select(state.filter))
        container.add_item(select_row)
        last = discord.ui.ActionRow()
        last.add_item(self.make_button("stop", label="Stop", style=discord.ButtonStyle.danger))
        last.add_item(self.make_button("disconnect", label="Leave"))
        container.add_item(last)
        view.add_item(container)
        return view

    async def remove_panel(self, state: MusicState) -> None:
        async with state.panel_lock:
            message = state.panel_message
            if message is None:
                state.panel_track_id = None
                return
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                print(f"[music] could not remove panel message: {error}")
                return
            state.panel_message = None
            state.panel_track_id = None

    async def send_panel(self, player: wavelink.Player, state: MusicState) -> None:
        async with state.panel_lock:
            if not state.post_now_playing:
                message = state.panel_message
                state.panel_message = None
                state.panel_track_id = None
                if message is not None:
                    try:
                        await message.delete()
                    except discord.NotFound:
                        pass
                    except discord.HTTPException as error:
                        print(f"[music] could not remove disabled now-playing panel: {error}")
                return
            track = state.current
            if track is None:
                return
            view = self.build_view(player, state)
            none = discord.AllowedMentions.none()

            if state.panel_message is not None:
                if state.panel_track_id == track.identifier:
                    # Same song: refresh in place so volume and loop toggles do not spam chat.
                    try:
                        await state.panel_message.edit(view=view, allowed_mentions=none)
                        return
                    except discord.NotFound:
                        state.panel_message = None
                        state.panel_track_id = None
                    except discord.HTTPException as error:
                        print(f"[music] could not update panel message: {error}")
                        return
                else:
                    # New song: drop the old panel first. Editing it would leave it
                    # stranded higher up while newer messages pile up below it, and
                    # posting without deleting would stack a second panel per song.
                    try:
                        await state.panel_message.delete()
                    except discord.NotFound:
                        pass
                    except discord.HTTPException as error:
                        # Cannot delete, so posting again would leave a duplicate
                        # behind. Keep the old panel and update it in place instead.
                        print(f"[music] could not remove panel for the previous song: {error}")
                        try:
                            await state.panel_message.edit(view=view, allowed_mentions=none)
                        except (discord.NotFound, discord.HTTPException):
                            state.panel_message = None
                            state.panel_track_id = None
                        else:
                            state.panel_track_id = track.identifier
                        return
                    state.panel_message = None
                    state.panel_track_id = None

            channel = self.bot.get_channel(state.text_channel_id) if state.text_channel_id else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                print(f"[music] panel channel is not a text/voice channel: {state.text_channel_id}")
                return
            try:
                state.panel_message = await channel.send(
                    view=view, files=self.panel_files(), allowed_mentions=none
                )
                state.panel_track_id = track.identifier
            except discord.HTTPException as error:
                print(f"[music] could not send panel: {error}")
                state.panel_message = None

    # --------------------------- playback engine ---------------------------
    async def start_track(self, player, state: MusicState, track, keep_prev: bool = False) -> bool:
        if not keep_prev and state.current is not None and state.current.identifier != track.identifier:
            state.prev = state.current
        state.current = track
        state.active = True
        state.started_at = time.monotonic()
        state.paused_at = None
        state.paused_total = 0.0
        state.offset_ms = 0
        state.desync_ticks = 0
        state.recent.append(track.identifier)
        try:
            await player.play(track, paused=False)
        except Exception as error:
            print(f"[music] could not start '{track.title}': {type(error).__name__}: {error}")
            return False
        if state.filter != "normal":
            try:
                await player.set_filters(build_filters(state.filter))
            except Exception as error:
                print(f"[music] could not keep filter '{state.filter}': {type(error).__name__}: {error}")
        return True

    async def advance(self, player, state: MusicState, reason: str) -> None:
        async with state.lock:
            await self._advance(player, state, reason)

    async def _advance(self, player, state: MusicState, reason: str) -> None:
        track = state.current
        if reason == "finished" and track is not None and state.loop == "track":
            if await self.start_track(player, state, track):
                return
        if track is not None and state.loop == "queue" and reason in ("finished", "skip"):
            player.queue.put(track)

        for _ in range(3):
            nxt = None
            if len(player.queue) > 0:
                nxt = player.queue.get()
            elif state.radio and track is not None:
                nxt = await self.pick_radio_track(state, track)
            if nxt is None:
                break
            if await self.start_track(player, state, nxt):
                return

        # Nothing left to play.
        state.active = False
        state.current = None
        if reason == "skip":
            try:
                await player.skip(force=True)
            except Exception:
                pass
        async with state.panel_lock:
            message = state.panel_message
            if message is not None:
                try:
                    view = self.build_view(player, state)
                    await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                except (discord.NotFound, discord.HTTPException):
                    state.panel_message = None
                    state.panel_track_id = None
            state.panel_track_id = None

    async def recover(self, player, state: MusicState, track, cause: str = "failed") -> None:
        """Retry a failed, stuck, or prematurely ended track before giving up on it."""
        async with state.lock:
            if state.current is None or state.current.identifier != track.identifier:
                return
            state.fail_streak += 1
            # Do not disable YouTube search sources because of individual
            # track failures; a single unavailable video must never block
            # ytsearch/ytmsearch for the whole guild.
            if state.fail_streak >= 10:
                state.radio = False
                state.active = False
                state.current = None
                player.queue.clear()
                state.attempts.clear()
                await self.remove_panel(state)
                source = self.preferred_source(state.guild_id)
                await self.notify(
                    state,
                    f"Playback stopped after repeated audio failures with "
                    f"`{source}`. Check the Lavalink logs for the source error; "
                    "the service may need a restart.",
                )
                return
            if state.fail_streak >= MAX_TRACK_ATTEMPTS:
                # Several failures in a row (often the same song returned from
                # different uploads) means the source/node is the problem, not one
                # file. Move on instead of restarting the same song again and again.
                state.attempts.pop(track.identifier, None)
                await self.notify(
                    state,
                    f"**{track.title}** was skipped after {state.fail_streak} failed attempts in a row.",
                )
                await self._advance(player, state, "failed")
                return
            key = track.identifier
            attempt = state.attempts.get(key, 0) + 1
            state.attempts[key] = attempt
            extra = getattr(track, "extra", None) or {}
            print(
                f"[music] {cause}: '{track.title}' source={track.source} "
                f"attempt {attempt}/{MAX_TRACK_ATTEMPTS} "
                f"node_reason={extra.get('reason') or extra.get('exception') or 'unknown'}"
            )
            if attempt > MAX_TRACK_ATTEMPTS:
                state.attempts.pop(key, None)
                because = {
                    "loadfailed": "the music node could not load it",
                    "stuck": "its audio stopped responding",
                }.get(cause, "it failed on the music node")
                await self.notify(
                    state,
                    f"**{track.title}** was skipped because {because} "
                    f"({MAX_TRACK_ATTEMPTS} retries, no other source has it).",
                )
                await self._advance(player, state, "failed")
                return
            # A stall is usually temporary, so ask the node for the same audio again first.
            if cause == "stuck" and attempt == 1 and await self.start_track(player, state, track, keep_prev=True):
                return
            query = state.search_queries.get(key)
            found = await self.find_fallback(track, query=query, state=state)
            if found is not None:
                alt, is_variant = found
                state.requesters[alt.identifier] = state.requesters.get(key)
                if query:
                    state.search_queries[alt.identifier] = query
                if await self.start_track(player, state, alt, keep_prev=True):
                    await self.notify(
                        state,
                        f"**{track.title}** could not be loaded, playing "
                        f"{'another version of' if is_variant else 'another copy of'} it.",
                    )
                    return
                print(f"[music] fallback failed to start: '{alt.title}'")
            # A source that ended early will repeat the same short fragment if
            # restarted, so skip it as soon as no full-length alternative is found.
            if cause == "premature":
                # First, try to find a fallback (existing logic)
                found = await self.find_fallback(track, query=query, state=state)
                if found is not None:
                    alt, is_variant = found
                    state.requesters[alt.identifier] = state.requesters.get(key)
                    if query:
                        state.search_queries[alt.identifier] = query
                    if await self.start_track(player, state, alt, keep_prev=True):
                        await self.notify(
                            state,
                            f"**{track.title}** could not be loaded, playing "
                            f"{'another version of' if is_variant else 'another copy of'} it.",
                        )
                        return
                    print(f"[music] fallback failed to start: '{alt.title}'")
                # If no fallback found, check if there are queue tracks
                if player.queue and len(player.queue) > 0:
                    # Advance to next queue track silently (no error message)
                    await self._advance(player, state, "skipped")
                    return
                # If no fallback found and no queue tracks, show error and advance
            # For non-premature causes OR premature without fallback AND no queue:
            state.attempts.pop(key, None)
            because = {
                "loadfailed": "the music node could not load it",
                "stuck": "its audio stopped responding",
                "premature": f"the {track.source or 'music source'} stream ended early",
            }.get(cause, "it failed on the music node")
            if cause == "premature":
                message = (
                    f"**{track.title}** was skipped because {because}; "
                    "no full-length alternative was found."
                )
            else:
                message = (
                    f"**{track.title}** was skipped because {because} "
                    f"(attempt {attempt}/{MAX_TRACK_ATTEMPTS}; no other source has it)."
                )
            await self.notify(
                state,
                message,
            )
            await self._advance(player, state, "failed")
            return

    @staticmethod
    def _norm(text: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    VARIANT_MARKERS = (
        "live", "remix", "cover", "karaoke", "instrumental", "acoustic", "sped up",
        "slowed", "nightcore", "mashup", "medley", "reprise", "unplugged", "demo",
        "lofi", "lo fi", "8d", "loop", "rework", "edit",
    )

    @classmethod
    def _variant_markers(cls, title: str) -> set[str]:
        padded = f" {title} "
        return {marker for marker in cls.VARIANT_MARKERS if f" {marker} " in padded}

    def _match_score(self, original, candidate) -> int:
        """How well a candidate matches the failed track. 0 means it is a different song."""
        left, right = self._norm(getattr(original, "title", "")), self._norm(getattr(candidate, "title", ""))
        if not left or not right:
            return 0
        left_core = " ".join(
            word for word in left.split() if word not in FILLER_QUERY_TOKENS
        )
        right_core = " ".join(
            word for word in right.split() if word not in FILLER_QUERY_TOKENS
        )
        title = 0
        if left == right or (left_core and left_core == right_core):
            title = 3
        elif (len(right.split()) >= 3 and left in right) or (len(left.split()) >= 3 and right in left):
            title = 2
        else:
            words_left, words_right = set(left.split()), set(right.split())
            shared = words_left & words_right
            if len(shared) >= 2 and len(shared) * 3 >= max(len(words_left), len(words_right)) * 2:
                title = 2
            elif len(shared) == 1:
                title = 1
        if not title:
            return 0

        author_left = self._norm(getattr(original, "author", ""))
        author_right = self._norm(getattr(candidate, "author", ""))
        author = 0
        if author_left and author_right:
            if author_left == author_right:
                author = 2
            elif set(author_left.split()) & set(author_right.split()):
                author = 1
            else:
                return 0

        duration = 0
        left_ms = getattr(original, "length", 0) or 0
        right_ms = getattr(candidate, "length", 0) or 0
        if left_ms > 0 and right_ms > 0:
            difference = abs(left_ms - right_ms)
            longest = max(left_ms, right_ms)
            if difference <= max(8_000, longest // 12):
                duration = 2
            elif difference <= max(25_000, longest // 4):
                duration = 1

        if not author and not duration:
            # Nothing to compare besides the name, so only an identical title is safe.
            return 4 if title == 3 else 0
        return title + author + duration

    def _same_song(self, original, candidate) -> bool:
        return self._match_score(original, candidate) >= 4

    def _same_artist(self, original, candidate) -> bool:
        left = self._norm(getattr(original, "author", ""))
        right = self._norm(getattr(candidate, "author", ""))
        if not left or not right:
            return False
        return left == right or bool(set(left.split()) & set(right.split()))

    def _pick_fallback(self, track, pool):
        """Best candidate from a search pool. Returns (track, is_variant) or None.

        A live/remix/karaoke cut is only used if the pool has no proper copy.
        """
        strict, strict_score, loose, loose_score = None, 0, None, 0
        original_markers = self._variant_markers(self._norm(getattr(track, "title", "")))
        for candidate in pool:
            score = self._match_score(track, candidate)
            if score < 4:
                continue
            if self._variant_markers(self._norm(candidate.title)) - original_markers:
                if self._same_artist(track, candidate) and score > loose_score:
                    loose, loose_score = candidate, score
            elif score > strict_score:
                strict, strict_score = candidate, score
        if strict is not None:
            return strict, False
        return (loose, True) if loose is not None else None

    async def find_fallback(self, track, *, query: str | None = None, state: MusicState | None = None):
        """Same song, different recording. A blocked YouTube video still has other videos of it."""
        source = getattr(track, "source", "")
        title = str(track.title or "").strip()
        author = str(track.author or "").strip()
        queries = list(dict.fromkeys(
            text for text in (
                f"{author} {title} full song".strip(),
                f"{author} {title}".strip(),
                query or "",
                title,
            ) if text
        ))
        seen = {track.identifier}
        pool = []
        for text in queries:
            tracks, _, _ = await self.search_tracks(text, state=state)
            for candidate in tracks:
                if candidate.identifier in seen or candidate.is_stream:
                    continue
                seen.add(candidate.identifier)
                pool.append(candidate)
            if any(self._match_score(track, item) >= 6 for item in pool):
                break

        found = self._pick_fallback(track, pool)
        if found is not None:
            best, variant = found
            print(
                f"[music] fallback: '{track.title}' -> '{best.title}' by '{best.author}' "
                f"variant={variant} duration={getattr(best, 'length', 0)}"
            )
            return best, variant
        print(f"[music] no fallback for '{track.title}' source={source} query={queries[0]!r}")
        return None

    async def pick_radio_track(self, state: MusicState, track):
        tracks, _, _ = await self.search_tracks(track.author or track.title, state=state)
        fresh = [t for t in tracks[:20] if t.identifier not in state.recent and not t.is_stream]
        pool = fresh or [t for t in tracks[:20] if not t.is_stream]
        if not pool:
            return None
        choice = random.choice(pool)
        state.requesters[choice.identifier] = None
        return choice

    async def search_tracks(
        self,
        query: str,
        *,
        preferred: str | None = None,
        exclude_sources: set[str] | None = None,
        state: MusicState | None = None,
    ):
        # Search providers in priority order. A lower-priority provider must
        # not outrank a usable result from the preferred provider.
        if preferred is None and state is not None:
            preferred = self.preferred_source(state.guild_id)
        preferred = preferred or SEARCH_SOURCE
        sources = [preferred] if URL_RE.match(query) else search_sources(preferred, state)
        if exclude_sources:
            sources = [source for source in sources if source not in exclude_sources]

        errors = []
        diagnostics = []

        for source in sources:
            try:
                results = await wavelink.Playable.search(query, source=source)
            except Exception as error:
                errors.append(f"{source}: {type(error).__name__}: {error}")
                diagnostics.append(f"{source}=error({type(error).__name__})")
                print(
                    f"[music] search source {source} failed "
                    f"({type(error).__name__}: {error})"
                )
                err_str = str(error).lower()
                is_rate_limit = any(
                    marker in err_str
                    for marker in ("429", "rate limit", "too many requests", "quota exceeded")
                )
                count = record_source_failure(
                    state, source, rate_limited=is_rate_limit
                )
                if count and (is_rate_limit or count >= 3):
                    print(
                        f"[music] source {source} cooled down after {count} error(s); "
                        f"rate_limit={is_rate_limit}"
                    )
                continue

            record_source_success(state, source)

            if isinstance(results, wavelink.Playlist):
                tracks = list(results.tracks)
                diagnostics.append(f"{source}={len(tracks)}")
                if tracks:
                    print(f"[music] search sources: {', '.join(diagnostics)}")
                    print(
                        f"[music] selected source: {source} "
                        f"(playlist, {len(tracks)} tracks)"
                    )
                    return tracks, results.name, None
                continue

            tracks = list(results)
            diagnostics.append(f"{source}={len(tracks)}")
            if not tracks:
                continue

            usable = (
                tracks if URL_RE.match(query)
                else ranked_search_results(tracks, query)
            )

            if usable:
                print(f"[music] search sources: {', '.join(diagnostics)}")
                print(
                    f"[music] selected source: {source} "
                    f"({len(usable)} usable result(s))"
                )
                return usable, None, None

            diagnostics[-1] += "-no-match"

        print(f"[music] search sources: {', '.join(diagnostics)}")
        if errors:
            return [], None, (
                "Search failed. Check the Lavalink node and its source plugins."
            )
        return [], None, None

    async def search_source(self, query: str, source: str):
        """Search one source. Returns (tracks, playlist_name, errored)."""
        try:
            results = await wavelink.Playable.search(query, source=source)
        except Exception as error:
            print(f"[music] source {source} search failed: {type(error).__name__}: {error}")
            return [], None, True
        if isinstance(results, wavelink.Playlist):
            return list(results.tracks), results.name, False
        return list(results), None, False

    async def youtube_oembed_search_query(self, url: str) -> str | None:
        """Get public YouTube video metadata for a title-search fallback."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            allowed = {
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "music.youtube.com", "youtu.be", "www.youtu.be",
                "youtube-nocookie.com", "www.youtube-nocookie.com",
            }
            if host not in allowed:
                return None

            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://www.youtube.com/oembed",
                    params={"url": url, "format": "json"},
                ) as response:
                    if response.status != 200:
                        print(f"[music] YouTube oEmbed HTTP {response.status}")
                        return None
                    payload = await response.json(content_type=None)

            title = payload.get("title")
            author = payload.get("author_name")
            if not isinstance(title, str) or not title.strip():
                return None

            query = " ".join(
                v.strip() for v in (title, author)
                if isinstance(v, str) and v.strip()
            )
            print(f"[music] YouTube URL metadata resolved: {title[:100]!r}")
            return query[:300]
        except Exception as error:
            print(f"[music] YouTube oEmbed fallback failed: {type(error).__name__}: {error}")
            return None

    async def resolve(self, query: str, state: MusicState | None = None):
        query = query.strip().strip("<>")
        preferred = self.preferred_source(state.guild_id if state is not None else None)

        if is_spotify_link(query):
            return [], None, (
                "Spotify links open in the Spotify app and cannot be relayed into a shared Discord voice channel by K.R.Y.O. "
                f"Open the link in Spotify: {spotify_open_url(query)}\nFor VC playback, use a YouTube or SoundCloud link, or search by song title."
            )
        if URL_RE.match(query):
            tracks, playlist_name, failed = await self.search_source(query, preferred)

            if playlist_name:
                return tracks[:MAX_QUEUE_TRACKS], playlist_name, None
            if tracks:
                return tracks[:1], None, None

            host = (urlparse(query).hostname or "").lower()
            youtube_hosts = {
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "music.youtube.com", "youtu.be", "www.youtu.be",
                "youtube-nocookie.com", "www.youtube-nocookie.com",
            }

            if host in youtube_hosts:
                fallback_query = await self.youtube_oembed_search_query(query)
                if fallback_query:
                    fallback_tracks, fallback_playlist, fallback_error = (
                        await self.search_tracks(
                            fallback_query, preferred=preferred, state=state
                        )
                    )
                    if fallback_playlist:
                        return fallback_tracks[:MAX_QUEUE_TRACKS], fallback_playlist, None
                    if fallback_tracks:
                        selected = preferred_search_result(fallback_tracks, fallback_query)
                        selected = selected or fallback_tracks[0]
                        print(
                            f"[music] YouTube URL fallback selected: "
                            f"{selected.title!r} by {selected.author!r}"
                        )
                        return [selected], None, None

                return [], None, (
                    "The original YouTube video could not be loaded, "
                    "and no playable title match was found."
                )

            if failed:
                return [], None, "Could not load that link from the audio provider. Check that the link is public and try again."
            return [], None, None

        tracks, playlist_name, error = await self.search_tracks(
            query, preferred=preferred, state=state
        )
        if error:
            return [], None, error
        if playlist_name:
            return tracks[:MAX_QUEUE_TRACKS], playlist_name, None
        selected = preferred_search_result(tracks, query)
        if selected is not None:
            print(f"[music] selected match: {selected.title!r} by {selected.author!r}")
            return [selected], None, None
        return [], None, None

    def resolve_panel_channel(self, guild, channel):
        """Return a channel suitable for sending the music panel.

        Prefer the voice channel's own built-in text chat when the bot has
        the required permissions. Fall back to a category text channel, the
        guild system channel, or any accessible text channel only when the
        voice-channel chat is unavailable.
        """
        bot_member = guild.me
        if isinstance(channel, discord.VoiceChannel):
            perms = channel.permissions_for(bot_member) if bot_member is not None else None
            if perms is None or (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
                return channel
            print(
                f"[music] voice channel chat unavailable guild={guild.id} channel={channel.id}: "
                f"view={perms.view_channel} send={perms.send_messages} embeds={perms.embed_links} files={perms.attach_files}"
            )
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        # channel is None or not usable: find a text channel
        candidates = []
        if channel is not None and getattr(channel, "category", None) is not None:
            for ch in channel.category.text_channels:
                if isinstance(ch, discord.TextChannel):
                    candidates.append(ch)
        if not candidates and guild.system_channel is not None:
            candidates.append(guild.system_channel)
        if not candidates:
            for ch in guild.text_channels:
                candidates.append(ch)
        for ch in candidates:
            if bot_member is None:
                return ch
            perms = ch.permissions_for(bot_member)
            if perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files:
                return ch
        return candidates[0] if candidates else None

    async def spotify_preview(self, query: str):
        """Fetch Spotify's public oEmbed metadata and return a link-only preview."""
        spotify_url = spotify_open_url(query) or query
        metadata = {}

        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://open.spotify.com/oembed",
                    params={"url": spotify_url},
                ) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        if isinstance(payload, dict):
                            metadata = payload
                    else:
                        print(f"[music] Spotify oEmbed returned HTTP {response.status}")
        except Exception as error:
            print(
                f"[music] Spotify preview metadata unavailable: "
                f"{type(error).__name__}: {error}"
            )

        title = metadata.get("title")
        if not isinstance(title, str) or not title.strip():
            title = "Spotify content"

        embed = discord.Embed(
            title=title.strip()[:256],
            url=spotify_url,
            description=(
                "This is a Spotify link preview. K.R.Y.O cannot relay Spotify audio "
                "into a shared Discord voice channel."
            ),
            color=discord.Color.from_rgb(29, 185, 84),
        )

        thumbnail = metadata.get("thumbnail_url")
        if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
            embed.set_thumbnail(url=thumbnail[:2048])

        embed.set_footer(text="Spotify • Link preview only")

        view = discord.ui.View(timeout=600)
        view.add_item(
            discord.ui.Button(
                label="Open in Spotify",
                style=discord.ButtonStyle.link,
                url=spotify_url,
            )
        )
        return embed, view

    async def play_query(self, guild, member, channel, query: str):
        """Returns a message to show, or None when the panel is enough."""
        if is_spotify_link(query):
            return await self.spotify_preview(query)
        player, error = await self.ensure_player(guild, member)
        if error:
            return error
        state = self.state(guild.id)
        panel_channel = self.resolve_panel_channel(guild, channel)
        state.text_channel_id = panel_channel.id if panel_channel is not None else (channel.id if channel is not None else None)
        tracks, playlist_name, error = await self.resolve(query, state)
        if error:
            return error
        if not tracks:
            return "No playable results were found for that search."

        return await self.queue_resolved_tracks(
            player,
            state,
            member,
            tracks,
            playlist_name=playlist_name,
            search_query=query if not URL_RE.match(query) else None,
        )

    async def play_selected_track(self, guild, member, channel, track, search_query: str):
        player, error = await self.ensure_player(guild, member)
        if error:
            return error
        state = self.state(guild.id)
        panel_channel = self.resolve_panel_channel(guild, channel)
        state.text_channel_id = panel_channel.id if panel_channel is not None else (channel.id if channel is not None else None)
        return await self.queue_resolved_tracks(
            player, state, member, [track], search_query=search_query
        )

    async def queue_resolved_tracks(
        self,
        player,
        state: MusicState,
        member,
        tracks: list,
        *,
        playlist_name: str | None = None,
        search_query: str | None = None,
    ):
        async with state.lock:
            for track in tracks:
                state.requesters[track.identifier] = member.id
                if search_query:
                    state.search_queries[track.identifier] = search_query
            busy = state.active and player.current is not None
            if not busy:
                state.active = False  # clear any stale flag
                first, rest = tracks[0], tracks[1:]
                if rest:
                    player.queue.put(rest)
                if not await self.start_track(player, state, first):
                    return "Could not start playback. Check the Lavalink node and its sources."
                if playlist_name:
                    return f"Loaded **{playlist_name}**: {len(tracks)} track(s)."
                return None
            player.queue.put(tracks)
        if playlist_name:
            return f"Added **{playlist_name}** ({len(tracks)} tracks) to the queue."
        return f"Added **{tracks[0].title}** to the queue."

    # ------------------------------ actions ------------------------------
    async def do_prev(self, player, state):
        if state.prev is None or state.current is None:
            return "There is no previous track."
        current, target = state.current, state.prev
        async with state.lock:
            queue_front(player.queue, current)
            if not await self.start_track(player, state, target):
                return "Could not go back to the previous track."
        return None

    async def do_pause(self, player, state):
        now = time.monotonic()
        if player.paused:
            await player.pause(False)
            if state.paused_at is not None:
                state.paused_total += now - state.paused_at
                state.paused_at = None
        else:
            await player.pause(True)
            state.paused_at = now
        return None

    async def do_resume(self, player, state):
        if not player.paused:
            return "Playback is not paused."
        return await self.do_pause(player, state)

    async def do_skip(self, player, state):
        if state.current is None:
            return "Nothing is playing right now."
        await self.advance(player, state, "skip")
        return None

    async def do_stop(self, player, state):
        state.active = False
        state.current = None
        player.queue.clear()
        try:
            await player.skip(force=True)
        except Exception:
            pass
        async with state.panel_lock:
            message = state.panel_message
            if message is not None:
                try:
                    view = self.build_view(player, state)
                    await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                except (discord.NotFound, discord.HTTPException):
                    state.panel_message = None
                    state.panel_track_id = None
            state.panel_track_id = None
        if state.stay_247:
            # The guild wants the bot to stay in voice 24/7: stop the music but
            # keep the connection so the channel is not abandoned.
            return "Stopped and cleared the queue. The bot stays in voice (24/7 mode)."
        await self.cleanup(player.guild.id, player)
        return "Stopped, cleared the queue, and left the voice channel."

    async def do_loop(self, player, state):
        state.loop = {"off": "track", "track": "queue", "queue": "off"}[state.loop]
        return None

    async def set_volume(self, player, value: int):
        await player.set_volume(max(0, min(200, value)))

    async def do_volup(self, player, state):
        await self.set_volume(player, player.volume + 10)
        return None

    async def do_voldown(self, player, state):
        await self.set_volume(player, player.volume - 10)
        return None

    async def seek_by(self, player, state, delta_ms: int):
        track = state.current
        if track is None or track.is_stream or not track.is_seekable:
            return "This track cannot be seeked."
        position = player.position
        target = max(0, min(position + delta_ms, track.length - 1000))
        try:
            await player.seek(target)
        except (wavelink.WavelinkException, discord.HTTPException) as music_error:
            print(f"[music] seek failed: {type(music_error).__name__}: {music_error}")
            return "Could not seek. The music server connection dropped."
        state.offset_ms += target - position
        return None

    async def do_forward(self, player, state):
        return await self.seek_by(player, state, 10_000)

    async def do_back(self, player, state):
        return await self.seek_by(player, state, -10_000)

    async def do_radio(self, player, state):
        state.radio = not state.radio
        return None

    async def do_shuffle(self, player, state):
        if len(player.queue) < 2:
            return "Not enough queued tracks to shuffle."
        player.queue.shuffle()
        return None

    async def do_disconnect(self, player, state):
        await self.cleanup(player.guild.id, player)
        return "Stopped and left the voice channel."

    async def cleanup(self, guild_id: int, player=None) -> None:
        state = self.states.get(guild_id)
        stay_247 = state.stay_247 if state is not None else False
        stay_channel_id = state.stay_channel_id if state is not None else None
        if state is not None:
            state.active = False
            await self.remove_panel(state)
        if not stay_247:
            self.states.pop(guild_id, None)
        else:
            # Preserve 24/7 settings so the stay loop can reconnect.
            st = self.states.setdefault(guild_id, MusicState())
            st.guild_id = guild_id
            st.stay_247 = True
            st.stay_channel_id = stay_channel_id
            st.active = False
            st.current = None
        if player is not None:
            try:
                await player.disconnect()
            except Exception:
                pass

    @staticmethod
    async def _safe_reply(interaction: discord.Interaction, content: str) -> None:
        """Reply to a button press without ever raising.

        Discord retries an interaction whose response was lost and also re-sends
        one that a user double-clicked, so the callback can arrive already
        acknowledged (40060) or expired (10062). Neither is a real failure, and
        letting it escape produced the generic "unexpected error" message.
        """
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            pass

    async def handle_button(self, interaction: discord.Interaction, action: str) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await self._safe_reply(interaction, "Use this inside a server.")
        player, state, error = self.control_player(guild, member)
        if error:
            return await self._safe_reply(interaction, error)
        if player is None or state is None:
            return await self._safe_reply(interaction, "The music player is unavailable right now.")
        if state.current is None and action != "disconnect":
            return await self._safe_reply(interaction, "Nothing is playing right now.")

        if action == "lyrics":
            if state.current is None:
                return await self._safe_reply(interaction, "Nothing is playing right now.")
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                return
            embed = await self.lyrics_embed(state.current)
            try:
                return await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                return

        if action == "queue":
            return await self._safe_reply(interaction, self.queue_text(player, state))

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # A duplicate press for a control that only changes playback state:
            # the first press already did the work, so drop this one.
            return
        try:
            note = await getattr(self, f"do_{action}")(player, state)
        except Exception as error_:
            print(f"[music] button '{action}' failed: {type(error_).__name__}: {error_}")
            note = "That control failed. Try again."
        if note:
            try:
                await interaction.followup.send(note, ephemeral=True)
            except discord.HTTPException:
                pass
        if action in IN_PLACE_ACTIONS and state.current is not None:
            try:
                await interaction.edit_original_response(view=self.build_view(player, state))
            except discord.HTTPException:
                pass

    async def apply_filter(self, player, name: str) -> str | None:
        """Apply a preset. Returns an error text for the user, or None when it worked."""
        needed = FILTER_REQUIRES.get(name)
        if needed:
            supported = None
            try:
                info = await player.node.fetch_info()
                supported = info.get("filters") if isinstance(info, dict) else getattr(info, "filters", None)
            except Exception as error:
                print(f"[music] could not read node filter support: {type(error).__name__}: {error}")
            if supported and needed not in supported:
                print(f"[music] node does not support the '{needed}' filter (supports: {supported}).")
                return (
                    f"This music server has the `{needed}` filter turned off, so "
                    f"**{FILTER_PRESETS[name][0]}** cannot work here. Use another Lavalink node or enable it in its config."
                )
        filters = build_filters(name)
        try:
            try:
                await player.set_filters(filters, seek=True)
            except TypeError:
                await player.set_filters(filters)
        except Exception as error:
            print(f"[music] filter '{name}' failed: {type(error).__name__}: {error}")
            return "That audio filter could not be applied. Try again in a moment."
        return None

    async def handle_filter(self, interaction: discord.Interaction, value: str) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return
        player, state, error = self.control_player(guild, member)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if player is None or state is None:
            await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
            return
        if state.current is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        if value not in FILTER_PRESETS:
            value = "normal"
        await interaction.response.defer()
        problem = await self.apply_filter(player, value)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        state.filter = value
        try:
            await interaction.edit_original_response(view=self.build_view(player, state))
        except discord.HTTPException:
            pass

    async def lyrics_embed(self, track) -> discord.Embed:
        title = re.sub(r"[\(\[].*?[\)\]]", "", track.title).strip() or track.title
        query = f"{track.author} {title}".strip()
        text = None
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://lrclib.net/api/search", params={"q": query}) as response:
                    if response.status == 200:
                        for item in await response.json():
                            if item.get("plainLyrics"):
                                text = item["plainLyrics"]
                                break
        except Exception:
            text = None
        if not text:
            return discord.Embed(
                title="Lyrics not found",
                description=f"No lyrics were found for **{track.title}**.",
                color=discord.Color.orange(),
            )
        return discord.Embed(title=f"Lyrics: {track.title}"[:250], description=text[:4000], color=PANEL_COLOR)

    # ------------------------------ events ------------------------------
    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        state = self.states.get(player.guild.id)
        if state is None or state.current is None or state.current.identifier != payload.track.identifier:
            return
        # NOTE: this event also fires when recover() replays a track. Clearing the
        # retry counters here (as an earlier version did) reset the attempt limit on
        # every replay, so a track that kept ending early looped forever and the bot
        # never moved on. The counters are cleared only when a track really finishes.
        state.desync_ticks = 0
        await self.send_panel(player, state)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if player is None or player.guild is None or payload.track is None:
            return
        print(
            f"[music] track ended guild={player.guild.id} "
            f"title={payload.track.title!r} length_ms={payload.track.length} "
            f"reason={payload.reason}"
        )
        state = self.states.get(player.guild.id)
        if state is None or not state.active or state.current is None:
            return
        if payload.track.identifier != state.current.identifier:
            return  # old event from a track that was already replaced
        reason = str(payload.reason or "").lower().replace("_", "")
        if reason in ("replaced", "stopped", "cleanup"):
            return
        if reason == "loadfailed":
            await self.recover(player, state, payload.track, cause="loadfailed")
            return
        if reason == "finished" and payload.track.length > 0:
            elapsed_ms = (
                time.monotonic() - state.started_at - state.paused_total
            ) * 1000 + state.offset_ms
            if state.paused_at is not None:
                elapsed_ms -= (time.monotonic() - state.paused_at) * 1000
            if (
                elapsed_ms < payload.track.length * 0.8
                and payload.track.length - elapsed_ms > 20_000
            ):
                print(
                    f"[music] premature end guild={player.guild.id} "
                    f"title={payload.track.title!r} source={payload.track.source} "
                    f"elapsed_ms={int(elapsed_ms)} length_ms={payload.track.length}"
                )
                await self.recover(player, state, payload.track, cause="premature")
                return
        # The track reached its end on its own, which means playback is healthy.
        # Reset the failure bookkeeping recover() counts on, so one bad track cannot
        # leave the player stuck retrying it.
        state.fail_streak = 0
        state.attempts.pop(payload.track.identifier, None)
        await self.advance(player, state, "finished")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        guild_id = player.guild.id if player is not None and player.guild is not None else None
        print(
            f"[music] track exception guild={guild_id} "
            f"title={payload.track.title!r}: {payload.exception}"
        )
        if player is None or player.guild is None:
            return
        state = self.states.get(player.guild.id)
        if (
            state is None
            or not state.active
            or state.current is None
            or state.current.identifier != payload.track.identifier
        ):
            return
        await self.recover(player, state, payload.track, cause="exception")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        state = self.states.get(player.guild.id)
        if state is None or not state.active:
            return
        print(
            f"[music] track stuck: '{payload.track.title}' "
            f"guild={player.guild.id} source={payload.track.source} "
            f"threshold={getattr(payload, 'threshold', 'unknown')}ms"
        )
        await self.recover(player, state, payload.track, cause="stuck")

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if player.guild is None:
            return
        state = self.states.get(player.guild.id)
        if state is not None and state.stay_247:
            # The guild opted in to 24/7 presence: an idle timeout is not a reason
            # to leave. The stay loop will rejoin if the channel is ever lost.
            return
        if state is not None:
            await self.notify(state, "Left the voice channel because nothing was playing.")
        await self.cleanup(player.guild.id, player)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        # The bot itself was disconnected/kicked from voice: reset the stale state.
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        print(f"[music] voice state update guild={member.guild.id} before={getattr(before.channel, 'id', None)} after={getattr(after.channel, 'id', None)}")
        if before.channel is not None and after.channel is None:
            state = self.states.get(member.guild.id)
            if state is not None and state.stay_247:
                # Keep the flag; the stay loop will rejoin the target channel.
                state.active = False
                state.current = None
                await self.remove_panel(state)
                return
            await self.cleanup(member.guild.id)

    @tasks.loop(seconds=10)
    async def watchdog(self) -> None:
        for player in list(self.bot.voice_clients):
            try:
                if not isinstance(player, wavelink.Player) or player.guild is None:
                    continue
                state = self.states.get(player.guild.id)
                if state is None or not state.active or state.current is None:
                    continue
                track = state.current
                if player.current is None:
                    state.desync_ticks += 1
                    if state.desync_ticks >= 2:
                        state.desync_ticks = 0
                        await self.advance(player, state, "finished")
                    continue
                state.desync_ticks = 0
                if player.paused or track.is_stream or state.paused_at is not None:
                    continue
                elapsed_ms = (time.monotonic() - state.started_at - state.paused_total) * 1000 + state.offset_ms
                if elapsed_ms > track.length + 45_000:
                    await self.advance(player, state, "finished")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One broken guild must not stop the watchdog for every other one.
                print(f"[music] watchdog pass failed: {type(error).__name__}: {error}")

    @tasks.loop(seconds=PANEL_REFRESH_SECONDS)
    async def panel_ticker(self) -> None:
        """Keep the progress bar moving while a song is playing."""
        for guild_id, state in list(self.states.items()):
            try:
                if state.panel_message is None or state.current is None or not state.active:
                    continue
                guild = self.bot.get_guild(guild_id)
                player = guild.voice_client if guild is not None else None
                if not isinstance(player, wavelink.Player) or player.paused:
                    continue
                await self.send_panel(player, state)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[music] panel refresh failed: {type(error).__name__}: {error}")
            await asyncio.sleep(0.5)

    @panel_ticker.before_loop
    async def before_panel_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @watchdog.before_loop
    async def before_watchdog(self) -> None:
        await self.bot.wait_until_ready()

    # --------------------------- prefix commands ---------------------------
    @commands.command(name="music", brief="Music controls and search settings.",
                      extras={"category": "Music", "usage": ".music source [ytmsearch|ytsearch|scsearch|bcsearch]", "public": False})
    @commands.guild_only()
    async def cmd_music(self, ctx: commands.Context, action: str | None = None, *, value: str | None = None):
        guild = ctx.guild
        if guild is None:
            return await ctx.send("This command can only be used in a server.")
        if action is None or action.lower() not in {"source", "blocked"}:
            return await ctx.send(
                embed=ui.info(
                    "Music",
                    "`.music source <ytmsearch|ytsearch|scsearch|bcsearch>` — change search source\n"
                    "`.music blocked` — show temporarily blocked sources",
                )
            )
        state = self.state(guild.id)
        if action.lower() == "source":
            if value is None or value not in {"ytmsearch", "ytsearch", "scsearch", "bcsearch"}:
                return await ctx.send(embed=ui.warn("Usage", "`.music source ytmsearch`, `ytsearch`, `scsearch`, or `bcsearch`"))
            state.blocked_sources.clear()
            state.source_failures.clear()
            self.source_prefs[guild.id] = value
            if value in {"ytsearch", "ytmsearch"}:
                return await ctx.send(
                    embed=ui.success("Search Source Updated", f"Music will now search using `{value}` first.")
                )
            # Tell the truth about what happens next: YouTube is skipped entirely,
            # so the datacenter-IP block can no longer fail the playback.
            return await ctx.send(
                embed=ui.success(
                    "Search Source Updated",
                    f"Music will try `{value}` first, then the configured fallback and other available audio sources.",
                )
            )
        if action.lower() == "blocked":
            now = time.monotonic()
            lines = []
            for source, expiry in state.blocked_sources.items():
                remaining = max(0, int(expiry - now))
                lines.append(f"`{source}` — blocked for {remaining}s more ({state.source_failures.get(source, 0)} failures)")
            if not lines:
                return await ctx.send(embed=ui.success("Blocked Sources", "No sources are cooling down. All configured audio sources are available."))
            return await ctx.send(embed=ui.warn("Blocked Sources", "\n".join(lines) + "\nUse `.music source scsearch` to prefer SoundCloud."))

    @commands.command(name="play", aliases=["p"])
    @commands.guild_only()
    @commands.cooldown(3, 8, commands.BucketType.user)
    async def cmd_play(self, ctx: commands.Context, *, query: str):
        guild = ctx.guild
        if guild is None or not isinstance(ctx.author, discord.Member):
            return await ctx.send("Join a server voice channel to use music.")
        async with ctx.typing():
            message = await self.play_query(guild, ctx.author, ctx.channel, query)
        if isinstance(message, tuple) and len(message) == 2 and isinstance(message[0], discord.Embed):
            await ctx.send(embed=message[0], view=message[1])
        elif message:
            await ctx.send(message)

    async def _prefix_control(self, ctx: commands.Context, action: str):
        player, state, error = self.control_player(ctx.guild, ctx.author)
        if error:
            return await ctx.send(error)
        if player is None or state is None:
            return await ctx.send("The music player is unavailable right now.")
        if state.current is None and action not in ("disconnect",):
            return await ctx.send("Nothing is playing right now.")
        try:
            note = await getattr(self, f"do_{action}")(player, state)
        except (wavelink.WavelinkException, discord.HTTPException, asyncio.TimeoutError) as music_error:
            print(f"[music] {action} failed: {type(music_error).__name__}: {music_error}")
            note = "Music server connection lost. Try again in a moment."
        if note:
            await ctx.send(note)

    @commands.command(name="pause")
    @commands.guild_only()
    async def cmd_pause(self, ctx: commands.Context):
        await self._prefix_control(ctx, "pause")

    @commands.command(name="resume")
    @commands.guild_only()
    async def cmd_resume(self, ctx: commands.Context):
        await self._prefix_control(ctx, "resume")

    @commands.command(name="skip")
    @commands.guild_only()
    async def cmd_skip(self, ctx: commands.Context):
        await self._prefix_control(ctx, "skip")

    @commands.command(name="stop")
    @commands.guild_only()
    async def cmd_stop(self, ctx: commands.Context):
        await self._prefix_control(ctx, "stop")

    @commands.command(name="disconnect", aliases=["dc"])
    @commands.guild_only()
    async def cmd_disconnect(self, ctx: commands.Context):
        await self._prefix_control(ctx, "disconnect")

    @commands.command(name="volume")
    @commands.guild_only()
    async def cmd_volume(self, ctx: commands.Context, percent: commands.Range[int, 1, 200]):
        player, state, error = self.control_player(ctx.guild, ctx.author)
        if error:
            return await ctx.send(error)
        if player is None or state is None:
            return await ctx.send("The music player is unavailable right now.")
        try:
            await self.set_volume(player, percent)
        except (wavelink.WavelinkException, discord.HTTPException) as music_error:
            print(f"[music] volume failed: {type(music_error).__name__}: {music_error}")
            return await ctx.send("Music server connection lost. Try again in a moment.")
        await ctx.send(f"Volume set to {percent}%.")

    @commands.command(name="queue", aliases=["q"])
    @commands.guild_only()
    async def cmd_queue(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            return await ctx.send("This command can only be used in a server.")
        player = guild.voice_client
        if not isinstance(player, wavelink.Player):
            return await ctx.send("The bot is not in a voice channel.")
        await ctx.send(self.queue_text(player, self.state(guild.id)))

    @commands.command(name="nowplaying", aliases=["np"])
    @commands.guild_only()
    async def cmd_nowplaying(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            return await ctx.send("This command can only be used in a server.")
        player = guild.voice_client
        state = self.state(guild.id)
        if not isinstance(player, wavelink.Player) or state.current is None:
            return await ctx.send("Nothing is playing right now.")
        state.text_channel_id = ctx.channel.id
        await self.send_panel(player, state)

    @commands.command(name="music247", brief="24/7 voice presence toggle.",
                      extras={"category": "Music", "usage": ".music247 on|off|channel|status", "public": False})
    @commands.guild_only()
    async def cmd_music247(self, ctx: commands.Context, action: str | None = None, *, value: str | None = None):
        guild = ctx.guild
        if guild is None or not isinstance(ctx.author, discord.Member):
            return await ctx.send("This command can only be used in a server.")
        state = self.state(guild.id)
        if action is None or action.lower() not in {"on", "off", "channel", "status"}:
            return await ctx.send(
                embed=ui.info(
                    "24/7 Voice Presence",
                    "`.music247 on` — stay in voice 24/7, even when idle\n"
                    "`.music247 off` — leave when nothing is playing (default)\n"
                    "`.music247 channel <name|id>` — pick which voice channel to stay in\n"
                    "`.music247 status` — show current settings",
                )
            )
        action = action.lower()
        if action == "status":
            target = self.bot.get_channel(state.stay_channel_id) if state.stay_channel_id else None
            target_name = f"<#{target.id}>" if target is not None else "current channel"
            return await ctx.send(
                embed=ui.info(
                    "24/7 Status",
                    f"Mode: **{'ON' if state.stay_247 else 'OFF'}**\n"
                    f"Stay channel: {target_name}",
                )
            )
        if action == "off":
            state.stay_247 = False
            state.stay_channel_id = None
            self.persist_247_settings()
            return await ctx.send(embed=ui.success("24/7 Disabled", "The bot will leave when nothing is playing."))
        if action == "channel":
            if value is None:
                return await ctx.send(embed=ui.warn("Usage", "`.music247 channel <channel name or id>`"))
            channel = self._resolve_voice_channel(guild, value)
            if channel is None:
                return await ctx.send(embed=ui.warn("Channel Not Found", f"Could not find a voice channel matching `{value}`."))
            state.stay_channel_id = channel.id
            self.persist_247_settings()
            player = guild.voice_client
            if isinstance(player, wavelink.Player):
                try:
                    await player.move_to(channel)
                except Exception as error:
                    print(f"[music] music247 move failed: {type(error).__name__}: {error}")
            return await ctx.send(embed=ui.success("24/7 Channel Set", f"The bot will stay in {channel.mention}."))
        # action == "on"
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            return await ctx.send(embed=ui.warn("Join a Voice Channel", "You must be in a voice channel to enable 24/7."))
        state.stay_247 = True
        if state.stay_channel_id is None:
            state.stay_channel_id = voice.channel.id
        self.persist_247_settings()
        return await ctx.send(
            embed=ui.success(
                "24/7 Enabled",
                "The bot will stay in voice 24/7 and rejoin if it is disconnected. "
                "It will never leave while this is on.",
            )
        )

    @staticmethod
    def _resolve_voice_channel(guild: discord.Guild, value: str) -> discord.VoiceChannel | None:
        value = value.strip().lstrip("<#").rstrip(">")
        if value.isdigit():
            channel = guild.get_channel(int(value))
            if isinstance(channel, discord.VoiceChannel):
                return channel
        for channel in guild.voice_channels:
            if channel.name.lower() == value.lower():
                return channel
        return None

    def queue_text(self, player, state: MusicState) -> str:
        lines = []
        if state.current is not None:
            length = "LIVE" if state.current.is_stream else fmt_duration(state.current.length)
            lines.append(f"**Now playing:** {state.current.title} (`{length}`)")
        upcoming = list(player.queue)[:10]
        for index, track in enumerate(upcoming, start=1):
            length = "LIVE" if track.is_stream else fmt_duration(track.length)
            lines.append(f"{index}. {track.title} (`{length}`)")
        if not lines:
            return "The queue is empty."
        remaining = len(player.queue) - len(upcoming)
        if remaining > 0:
            lines.append(f"... and {remaining} more track(s)")
        return "\n".join(lines)[:1900]

    # ---------------------------- slash commands ----------------------------
    @app_commands.command(
        name="play",
        description="Search for a song and choose a result to play",
    )
    @app_commands.guild_only()
    @app_commands.describe(query="Start typing a song name to see matching tracks")
    async def slash_play(self, interaction: discord.Interaction, query: str):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Discord retries an interaction whose first response was lost. The
            # retry arrives already acknowledged (40060) or expired (10062); the
            # original request is still being handled, so the retry is dropped
            # instead of raising an unhandled command error.
            return
        cached = self.autocomplete_tracks.pop(query, None)
        if (
            cached is not None
            and interaction.guild_id is not None
            and cached[0] == interaction.user.id
            and cached[1] == interaction.guild_id
            and cached[2] >= time.monotonic()
        ):
            message = await self.play_selected_track(
                interaction.guild,
                interaction.user,
                interaction.channel,
                cached[3],
                cached[4],
            )
        else:
            message = await self.play_query(
                interaction.guild, interaction.user, interaction.channel, query
            )
        try:
            if isinstance(message, tuple) and len(message) == 2 and isinstance(message[0], discord.Embed):
                await interaction.followup.send(embed=message[0], view=message[1])
            elif message is None:
                await interaction.delete_original_response()
            else:
                await interaction.followup.send(message)
        except discord.HTTPException as error:
            print(f"[music] playback response could not be completed: {type(error).__name__}: {error}")

    #: Discord kills an autocomplete response after 3 seconds and then retries the
    #: interaction, which is what produced the "already acknowledged" / "Unknown
    #: interaction" errors. Search is capped well under that budget, and a slow or
    #: unresponsive Lavalink node degrades to "no suggestions" instead of an error.
    AUTOCOMPLETE_TIMEOUT = 2.0

    @slash_play.autocomplete("query")
    async def autocomplete_play(self, interaction: discord.Interaction, current: str):
        current = current.strip()
        if len(current) < 2 or URL_RE.match(current) or not self.node_ready():
            return []

        # Honour the guild's chosen source here too, so suggestions come from the
        # same place playback will use (and never from blocked YouTube).
        try:
            tracks, _, error = await asyncio.wait_for(
                self.search_tracks(
                    current,
                    preferred=self.preferred_source(interaction.guild_id),
                ),
                timeout=self.AUTOCOMPLETE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return []
        except Exception as error:
            print(f"[music] autocomplete search failed: {type(error).__name__}: {error}")
            return []
        if error or not tracks:
            return []

        now = time.monotonic()
        self.autocomplete_tracks = {
            token: entry
            for token, entry in self.autocomplete_tracks.items()
            if entry[2] >= now
        }
        choices = []
        seen = set()
        for track in ranked_search_results(tracks, current):
            if track.identifier in seen:
                continue
            seen.add(track.identifier)
            token = uuid.uuid4().hex
            self.autocomplete_tracks[token] = (
                interaction.user.id,
                interaction.guild_id or 0,
                now + 300,
                track,
                current,
            )
            duration = "LIVE" if track.is_stream else fmt_duration(track.length)
            label = f"{track.title} — {track.author} · {duration}"[:100]
            choices.append(app_commands.Choice(name=label, value=token))
            if len(choices) == 25:
                break

        if len(self.autocomplete_tracks) > 500:
            oldest = sorted(
                self.autocomplete_tracks,
                key=lambda token: self.autocomplete_tracks[token][2],
            )
            for token in oldest[: len(self.autocomplete_tracks) - 500]:
                self.autocomplete_tracks.pop(token, None)
        return choices

    async def _slash_control(self, interaction: discord.Interaction, action: str, done: str | None = None):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        if state.current is None and action != "disconnect":
            return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            note = await getattr(self, f"do_{action}")(player, state)
        except (wavelink.WavelinkException, discord.HTTPException, asyncio.TimeoutError) as music_error:
            print(f"[music] {action} failed: {type(music_error).__name__}: {music_error}")
            note = "Music server connection lost. Try again in a moment."
        await interaction.followup.send(note or done or "Done.", ephemeral=True)

    @music.command(name="pause", description="Pause the current track")
    async def slash_pause(self, interaction: discord.Interaction):
        await self._slash_control(interaction, "pause", "Toggled pause.")

    @music.command(name="resume", description="Resume the current track")
    async def slash_resume(self, interaction: discord.Interaction):
        await self._slash_control(interaction, "resume", "Resumed.")

    @music.command(name="skip", description="Skip the current track")
    async def slash_skip(self, interaction: discord.Interaction):
        await self._slash_control(interaction, "skip", "Skipped.")

    @music.command(name="stop", description="Stop music and clear the queue")
    async def slash_stop(self, interaction: discord.Interaction):
        await self._slash_control(interaction, "stop")

    @music.command(name="disconnect", description="Make the bot leave the voice channel")
    async def slash_disconnect(self, interaction: discord.Interaction):
        await self._slash_control(interaction, "disconnect", "Disconnected.")

    @music.command(name="queue", description="Show now playing and queued tracks")
    async def slash_queue(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        player = guild.voice_client
        if not isinstance(player, wavelink.Player):
            return await interaction.response.send_message("The bot is not in a voice channel.", ephemeral=True)
        await interaction.response.send_message(self.queue_text(player, self.state(guild.id)))

    @music.command(name="nowplaying", description="Show the music control panel again")
    async def slash_nowplaying(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        player = guild.voice_client
        state = self.state(guild.id)
        if not isinstance(player, wavelink.Player) or state.current is None:
            return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        panel_channel = self.resolve_panel_channel(guild, interaction.channel)
        state.text_channel_id = panel_channel.id if panel_channel is not None else interaction.channel_id
        await interaction.response.send_message("Panel refreshed.", ephemeral=True)
        await self.send_panel(player, state)

    music247 = app_commands.Group(
        name="music247",
        description="24/7 voice presence settings",
        guild_only=True,
    )

    @music247.command(name="status", description="Show 24/7 voice presence settings")
    async def slash_music247_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        state = self.state(guild.id)
        target = self.bot.get_channel(state.stay_channel_id) if state.stay_channel_id else None
        target_name = f"<#{target.id}>" if target is not None else "current channel"
        await interaction.response.send_message(
            embed=ui.info(
                "24/7 Status",
                f"Mode: **{'ON' if state.stay_247 else 'OFF'}**\n"
                f"Stay channel: {target_name}",
            ),
            ephemeral=True,
        )

    @music247.command(name="on", description="Keep the bot in voice 24/7, even when idle")
    async def slash_music247_on(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                embed=ui.warn("Join a Voice Channel", "You must be in a voice channel to enable 24/7."),
                ephemeral=True,
            )
            return
        state = self.state(guild.id)
        state.stay_247 = True
        if state.stay_channel_id is None:
            state.stay_channel_id = member.voice.channel.id
        self.persist_247_settings()
        await interaction.response.send_message(
            embed=ui.success(
                "24/7 Enabled",
                "The bot will stay in voice 24/7 and rejoin if it is disconnected. "
                "It will never leave while this is on.",
            ),
            ephemeral=True,
        )

    @music247.command(name="off", description="Let the bot leave when nothing is playing")
    async def slash_music247_off(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        state = self.state(guild.id)
        state.stay_247 = False
        state.stay_channel_id = None
        self.persist_247_settings()
        await interaction.response.send_message(
            embed=ui.success("24/7 Disabled", "The bot will leave when nothing is playing."),
            ephemeral=True,
        )

    @music247.command(name="channel", description="Pick which voice channel the bot stays in")
    @app_commands.describe(channel="The voice channel the bot will stay in 24/7")
    async def slash_music247_channel(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        state = self.state(guild.id)
        state.stay_channel_id = channel.id
        self.persist_247_settings()
        player = guild.voice_client
        if isinstance(player, wavelink.Player):
            try:
                await player.move_to(channel)
            except Exception as error:
                print(f"[music] music247 move failed: {type(error).__name__}: {error}")
        await interaction.response.send_message(
            embed=ui.success("24/7 Channel Set", f"The bot will stay in {channel.mention}."),
            ephemeral=True,
        )

    @music.command(name="volume", description="Set playback volume")
    async def slash_volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 200]):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        try:
            await self.set_volume(player, percent)
        except (wavelink.WavelinkException, discord.HTTPException) as music_error:
            print(f"[music] volume failed: {type(music_error).__name__}: {music_error}")
            return await interaction.response.send_message(
                "Music server connection lost. Try again in a moment.", ephemeral=True
            )
        await interaction.response.send_message(f"Volume set to {percent}%.", ephemeral=True)

    @music.command(name="shuffle", description="Shuffle the queued tracks")
    async def slash_shuffle(self, interaction: discord.Interaction):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        if len(player.queue) < 2:
            return await interaction.response.send_message("Not enough queued tracks to shuffle.", ephemeral=True)
        player.queue.shuffle()
        await interaction.response.send_message("Queue shuffled.", ephemeral=True)

    @music.command(name="loop", description="Set repeat mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Current track", value="track"),
            app_commands.Choice(name="Whole queue", value="queue"),
        ]
    )
    async def slash_loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        state.loop = mode.value
        await interaction.response.send_message(f"Loop mode: **{mode.name}**.", ephemeral=True)

    @music.command(name="seek", description="Jump to a position in the current track")
    async def slash_seek(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 86400]):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        track = state.current
        if track is None or track.is_stream or not track.is_seekable:
            return await interaction.response.send_message("This track cannot be seeked.", ephemeral=True)
        position = player.position
        target = max(0, min(seconds * 1000, track.length - 1000))
        try:
            await player.seek(target)
        except (wavelink.WavelinkException, discord.HTTPException) as music_error:
            print(f"[music] seek failed: {type(music_error).__name__}: {music_error}")
            return await interaction.response.send_message(
                "Could not seek. The music server connection dropped.", ephemeral=True
            )
        state.offset_ms += target - position
        await interaction.response.send_message(f"Jumped to {fmt_duration(target)}.", ephemeral=True)

    @music.command(name="remove", description="Remove a queued track by position")
    async def slash_remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        if position > len(player.queue):
            return await interaction.response.send_message("That queue position does not exist.", ephemeral=True)
        track = player.queue[position - 1]
        player.queue.delete(position - 1)
        await interaction.response.send_message(f"Removed **{track.title}**.", ephemeral=True)

    @music.command(name="clear", description="Clear all queued tracks")
    async def slash_clear(self, interaction: discord.Interaction):
        player, state, error = self.control_player(interaction.guild, interaction.user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if player is None or state is None:
            return await interaction.response.send_message("The music player is unavailable right now.", ephemeral=True)
        player.queue.clear()
        await interaction.response.send_message("Queue cleared.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    # Replace the old music commands and the old track-end handler from main.py.
    for name in ("play", "pause", "resume", "skip", "stop", "queue", "nowplaying", "volume"):
        bot.remove_command(name)
    bot.tree.remove_command("music")
    if hasattr(bot, "on_wavelink_track_end"):
        try:
            delattr(bot, "on_wavelink_track_end")
        except AttributeError:
            pass
    await bot.add_cog(MusicPanel(bot))
