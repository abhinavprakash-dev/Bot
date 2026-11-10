import asyncio
import random
import re
import zlib
from typing import Awaitable, Callable, Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from botcore import ui

URL_RE = re.compile(r"https?://", re.I)


def fmt_time(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds // 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def track_link(track: wavelink.Playable) -> str:
    title = discord.utils.escape_markdown(track.title or "Unknown title")
    return f"[{title}]({track.uri})" if track.uri else title


ACCENT = 0x8B7CF6
WAVE_CHARS = "▁▂▃▄▅▆▇█"

# key: (label, description)
FILTER_PRESETS = {
    "normal": ("Normal", "No audio effects"),
    "bassboost": ("Bass boost", "Heavier low end"),
    "nightcore": ("Nightcore", "Faster and higher pitch"),
    "vaporwave": ("Vaporwave", "Slower and deeper"),
    "8d": ("8D audio", "Sound rotates around you"),
    "karaoke": ("Karaoke", "Reduces vocals"),
}


def build_filters(name: str) -> wavelink.Filters:
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


def clean_text(text: Optional[str], limit: int = 100) -> str:
    text = " ".join((text or "").split())
    text = text.replace("[", "(").replace("]", ")")
    text = discord.utils.escape_markdown(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def waveform(seed_text: str, size: int = 26) -> str:
    """Decorative waveform, always the same for the same song."""
    rng = random.Random(zlib.crc32((seed_text or "kryo").encode("utf-8", "ignore")))
    level = rng.randint(2, 5)
    out = []
    for _ in range(size):
        steps = (-2, -1, -1, 0, 1) if level >= 6 else (-1, 0, 1, 1, 2) if level <= 1 else (-2, -1, -1, 0, 1, 1, 2)
        level = max(0, min(len(WAVE_CHARS) - 1, level + rng.choice(steps)))
        out.append(WAVE_CHARS[level])
    return "".join(out)


def progress_bar(position: int, length: int, size: int = 16) -> str:
    ratio = 0.0 if length <= 0 else max(0.0, min(1.0, position / length))
    filled = int(ratio * size)
    return "▰" * filled + "▱" * (size - filled)


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot, lavalink_uri: str, lavalink_password: str, search_source: str) -> None:
        self.bot = bot
        self.uri = lavalink_uri
        self.password = lavalink_password
        self.source = search_source or "scsearch"
        self._connect_task: Optional[asyncio.Task] = None
        self._track_fallbacks: dict[int, dict[str, list[wavelink.Playable]]] = {}
        self._track_queries: dict[int, dict[str, str]] = {}
        self._retrying: set[int] = set()
        self._player_panels: dict[int, discord.Message] = {}
        self._filters: dict[int, str] = {}
        self._requesters: dict[str, str] = {}
        self._ticker_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- node handling
    @property
    def ready(self) -> bool:
        try:
            return any(node.status is wavelink.NodeStatus.CONNECTED for node in wavelink.Pool.nodes.values())
        except Exception:
            return False

    async def cog_load(self) -> None:
        try:
            self.bot.add_view(MusicPlayerView(self))
        except Exception as error:
            print(f"Music: could not register the player buttons ({type(error).__name__}: {error}).")
        self._ticker_task = asyncio.create_task(self._panel_ticker())
        if not self.uri or not self.password:
            print("Music: LAVALINK_URI or LAVALINK_PASSWORD is empty in .env, music is disabled.")
            return
        self._connect_task = asyncio.create_task(self._connect_loop())

    async def cog_unload(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()
        if self._ticker_task is not None:
            self._ticker_task.cancel()
        try:
            await wavelink.Pool.close()
        except Exception:
            pass

    async def _connect_loop(self) -> None:
        """Keep trying until the Lavalink node is connected (public nodes are sometimes down)."""
        attempt = 0
        while not self.bot.is_closed():
            if self.ready:
                await asyncio.sleep(30)
                continue
            attempt += 1
            try:
                node = wavelink.Node(identifier=f"main-{attempt}", uri=self.uri, password=self.password, retries=3)
                await wavelink.Pool.connect(client=self.bot, nodes=[node])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Music: Lavalink connection attempt {attempt} failed ({type(error).__name__}: {error}). Retrying in 5s.")
            else:
                if self.ready:
                    print(f"Music: connected to Lavalink at {self.uri} after {attempt} attempts")
                    attempt = 0  # Reset on success
            await asyncio.sleep(5)  # Faster retry

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        print(f"Music: Lavalink node '{payload.node.identifier}' is ready (resumed={payload.resumed}).")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        if payload.player is None or payload.player.guild is None:
            return
        fallbacks = self._track_fallbacks.get(payload.player.guild.id)
        if fallbacks is not None:
            fallbacks.pop(payload.track.encoded, None)
            if not fallbacks:
                self._track_fallbacks.pop(payload.player.guild.id, None)
        self._track_queries.get(payload.player.guild.id, {}).pop(payload.track.encoded, None)
        self._retrying.discard(payload.player.guild.id)
        await self.refresh_player_panel(payload.player.guild.id)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        guild_id = player.guild.id
        fallbacks = self._track_fallbacks.get(guild_id, {}).pop(payload.track.encoded, [])
        if fallbacks:
            fallback = fallbacks.pop(0)
            for track in reversed(fallbacks):
                player.queue.put_at(0, track)
            print(
                f"Music: track {payload.track.title!r} failed; "
                f"trying {len(fallbacks)} backup search result(s)."
            )
            try:
                await player.play(fallback)
            except (discord.HTTPException, wavelink.WavelinkException) as error:
                print(f"Music: fallback playback failed ({type(error).__name__}: {error}).")
            return
        else:
            print(
                f"Music: track {payload.track.title!r} failed "
                f"({payload.exception.get('message', 'unknown playback error')})."
            )
        if guild_id in self._retrying:
            return
        query = self._track_queries.get(guild_id, {}).pop(payload.track.encoded, "")
        if not query:
            query = f"{payload.track.author or ''} {payload.track.title or ''}".strip()
        if not query:
            return
        self._retrying.add(guild_id)
        try:
            result, _ = await self.search(query)
            candidates = list(result) if result and not isinstance(result, wavelink.Playlist) else []
            candidates = [track for track in candidates if track.encoded != payload.track.encoded and not track.is_stream]
            if candidates:
                await player.play(candidates[0])
                print(f"Music: recovered failed track with '{candidates[0].title}'.")
        except (discord.HTTPException, wavelink.WavelinkException) as error:
            print(f"Music: playback recovery failed ({type(error).__name__}: {error}).")
        finally:
            self._retrying.discard(guild_id)
        if not self._track_fallbacks.get(guild_id):
            self._track_fallbacks.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if player.guild is not None:
            # Only clear fallbacks/filters, don't disconnect immediately
            # if there are tracks in the queue to play
            has_queue = player.queue and len(player.queue) > 0
            self._track_fallbacks.pop(player.guild.id, None)
            self._filters.pop(player.guild.id, None)
            if not has_queue:
                # No more tracks to play, finish panel and disconnect
                await self.finish_player_panel(player.guild.id)
                try:
                    await player.disconnect()
                except Exception:
                    pass
            # If there are queue tracks, let the _panel_ticker or next command
            # handle playing the next track - don't forcibly disconnect

    # ------------------------------------------------------------------ core logic
    def offline_embed(self) -> discord.Embed:
        return ui.error(
            "Music Is Offline",
            "The music server is not connected right now. Please try again in a minute.\n"
            "If this keeps happening, the bot owner should check the Lavalink settings.",
        )

    def player_embed(self, player: wavelink.Player) -> discord.Embed:
        track = player.current
        if track is None:
            return ui.music("KRYO Music Player", "Nothing is playing right now.")
        duration = "Live" if track.is_stream else fmt_time(track.length)
        description = (
            f"Connected to {player.channel.mention}  •  **Duration:** {duration}\n"
            f"**Artist:** {discord.utils.escape_markdown(track.author or 'Unknown')}"
        )
        if not track.is_stream and track.length > 0:
            position = min(player.position, track.length)
            filled = min(20, int(position / track.length * 20))
            bar = f"{'▬' * filled}🔘{'▬' * (20 - filled)}"
            description += f"\n`{fmt_time(position)}` {bar} `{fmt_time(track.length)}`"
        embed = discord.Embed(
            title=discord.utils.escape_markdown(track.title or "Unknown title"),
            url=track.uri,
            description=description,
            color=0x18181B,
        )
        embed.set_author(name="KRYO MUSIC PLAYER")
        loop_names = {
            wavelink.QueueMode.normal: "Off",
            wavelink.QueueMode.loop: "Track",
            wavelink.QueueMode.loop_all: "Queue",
        }
        embed.set_footer(
            text=(
                f"{'Paused' if player.paused else 'Playing'}  •  "
                f"Volume {player.volume}%  •  "
                f"Loop {loop_names.get(player.queue.mode, 'Off')}  •  "
                f"Queue {len(player.queue)}"
            )
        )
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        return embed

    async def _edit_panel(self, message: discord.Message, **kwargs) -> None:
        """Edit through the bot token, because interaction webhook tokens expire after 15 minutes."""
        partial = self.bot.get_partial_messageable(message.channel.id).get_partial_message(message.id)
        await partial.edit(**kwargs)

    async def _update_panel(self, guild_id: int, message: discord.Message, view: "MusicPlayerView") -> bool:
        try:
            await self._edit_panel(message, view=view)
            return True
        except discord.HTTPException as error:
            if not isinstance(error, (discord.NotFound, discord.Forbidden)):
                print(f"Music: could not update the player panel ({type(error).__name__}: {error}).")
            self._player_panels.pop(guild_id, None)
            return False
        except Exception as error:
            print(f"Music: unexpected panel error ({type(error).__name__}: {error}).")
            self._player_panels.pop(guild_id, None)
            return False

    async def upsert_player_panel(
        self,
        guild: discord.Guild,
        create_message: Callable[[], Awaitable[discord.Message]],
    ) -> bool:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player) or player.current is None:
            return False
        message = self._player_panels.get(guild.id)
        if message is not None and await self._update_panel(guild.id, message, MusicPlayerView(self, player)):
            return False
        message = await create_message()
        self._player_panels[guild.id] = message
        return True

    async def refresh_player_panel(self, guild_id: int) -> None:
        message = self._player_panels.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        player = guild.voice_client if guild is not None else None
        if message is None or not isinstance(player, wavelink.Player) or player.current is None:
            return
        await self._update_panel(guild_id, message, MusicPlayerView(self, player))

    async def finish_player_panel(self, guild_id: int, title: str = "Playback finished", text: str = "Play another song to start a new session.") -> None:
        message = self._player_panels.pop(guild_id, None)
        if message is None:
            return
        try:
            await self._edit_panel(message, view=MusicPlayerView(self, finished=(title, text)))
        except Exception as error:
            if not isinstance(error, discord.NotFound):
                print(f"Music: could not close the player panel ({type(error).__name__}: {error}).")

    async def _panel_ticker(self) -> None:
        """Keep the progress bar moving while music is playing."""
        while not self.bot.is_closed():
            await asyncio.sleep(20)
            try:
                for guild_id in list(self._player_panels):
                    guild = self.bot.get_guild(guild_id)
                    player = guild.voice_client if guild is not None else None
                    if isinstance(player, wavelink.Player) and player.current is not None and not player.paused:
                        await self.refresh_player_panel(guild_id)
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Music: panel refresh loop error ({type(error).__name__}: {error}).")

    def _remember_requester(self, tracks, member: discord.Member) -> None:
        if len(self._requesters) > 2000:
            self._requesters.clear()
        for track in tracks:
            try:
                self._requesters[track.encoded] = member.display_name
            except Exception:
                pass

    async def get_player(self, guild: discord.Guild, member: Optional[discord.Member]) -> tuple[Optional[wavelink.Player], Optional[discord.Embed]]:
        if not self.ready:
            return None, self.offline_embed()
        player = guild.voice_client
        if player is not None:
            if not isinstance(player, wavelink.Player):
                return None, ui.error("Voice Busy", "Another voice session is already active in this server.")
            if member is not None and (member.voice is None or member.voice.channel != player.channel):
                return None, ui.warn("Different Voice Channel", f"I am already playing in {player.channel.mention}.")
            return player, None
        voice = member.voice if member is not None else None
        if voice is None or voice.channel is None:
            return None, ui.warn("Join A Voice Channel", "You need to be in a voice channel to play music.")
        channel = voice.channel
        permissions = channel.permissions_for(guild.me)
        if not (permissions.connect and permissions.speak):
            return None, ui.error("Missing Permissions", f"I need **Connect** and **Speak** permissions in {channel.mention}.")
        try:
            player = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=30)
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError, wavelink.WavelinkException) as error:
            return None, ui.error("Could Not Join", f"I could not join {channel.mention} ({type(error).__name__}).")
        player.autoplay = wavelink.AutoPlayMode.partial
        self._filters.pop(guild.id, None)
        try:
            player.inactive_timeout = 300
        except Exception:
            pass
        return player, None

    def get_control(self, guild: discord.Guild, member: Optional[discord.Member], need_current: bool = True) -> tuple[Optional[wavelink.Player], Optional[discord.Embed]]:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player):
            return None, ui.warn("Not Connected", "I am not in a voice channel.")
        if member is not None and (member.voice is None or member.voice.channel != player.channel):
            return None, ui.warn("Join My Voice Channel", f"Join {player.channel.mention} to control the music.")
        if need_current and player.current is None:
            return None, ui.warn("Nothing Playing", "There is no track playing right now.")
        return player, None

    async def search(self, query: str):
        if URL_RE.match(query):
            try:
                result = await wavelink.Playable.search(query)
            except Exception as error:
                return None, error
            return result, None
        sources = [self.source] + [
            s for s in ("ytmsearch", "ytsearch", "scsearch") if s != self.source
        ]
        last_error: Optional[Exception] = None
        for source in sources:
            try:
                result = await wavelink.Playable.search(query, source=source)
            except Exception as error:
                last_error = error
                continue
            if result:
                return result, None
        return None, last_error

    async def do_play(self, guild: discord.Guild, member: Optional[discord.Member], query: str) -> discord.Embed:
        player, problem = await self.get_player(guild, member)
        if problem is not None:
            return problem
        result, error = await self.search(query.strip())
        if not result:
            if error is not None:
                print(f"Music: search failed for {query!r}: {type(error).__name__}: {error}")
                return ui.error("Search Failed", "The music server could not load that request. Try another link or song name.")
            return ui.warn("No Results", f"I could not find anything for **{discord.utils.escape_markdown(query[:100])}**.")

        if isinstance(result, wavelink.Playlist):
            tracks = list(result.tracks)[:100]
            name = result.name
        else:
            tracks = [result[0]]
            if not URL_RE.match(query):
                backup_tracks = list(result[1:5])
                if backup_tracks:
                    self._track_fallbacks.setdefault(guild.id, {})[tracks[0].encoded] = backup_tracks
            self._track_queries.setdefault(guild.id, {})[tracks[0].encoded] = query.strip()
            name = None

        self._remember_requester(tracks, member)
        started = False
        if player.current is None:
            first = tracks.pop(0)
            try:
                await player.play(first)
            except (discord.HTTPException, wavelink.WavelinkException):
                self._track_fallbacks.get(guild.id, {}).pop(first.encoded, None)
                raise
            started = True
            head = first
        else:
            head = tracks[0]
        if tracks:
            player.queue.put(tracks)

        emb = ui.music("Now Playing" if started else "Added To Queue")
        if name:
            emb.description = f"Playlist **{discord.utils.escape_markdown(name)}**: {len(tracks) + (1 if started else 0)} tracks added.\nFirst track: {track_link(head)}"
        else:
            emb.description = track_link(head)
            emb.add_field(name="Artist", value=head.author or "Unknown", inline=True)
            emb.add_field(name="Duration", value="Live" if head.is_stream else fmt_time(head.length), inline=True)
        if head.artwork:
            emb.set_thumbnail(url=head.artwork)
        emb.set_footer(text=f"Requested by {member.display_name} • KRYO Music")
        return emb

    async def do_pause(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member)
        if problem is not None:
            return problem
        await player.pause(True)
        return ui.music("Playback Paused", "The music is paused. Use resume to continue.")

    async def do_resume(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member)
        if problem is not None:
            return problem
        await player.pause(False)
        return ui.music("Playback Resumed", "The music is playing again.")

    async def do_skip(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member)
        if problem is not None:
            return problem
        skipped = player.current
        await player.skip(force=True)
        return ui.music("Track Skipped", f"Skipped {track_link(skipped)}.")

    async def do_stop(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        player.queue.clear()
        self._track_fallbacks.pop(guild.id, None)
        self._track_queries.pop(guild.id, None)
        self._retrying.discard(guild.id)
        self._filters.pop(guild.id, None)
        await player.disconnect()
        return ui.music("Playback Stopped", "The queue was cleared and I left the voice channel.")

    async def do_queue(self, guild, member=None) -> discord.Embed:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player) or (player.current is None and not player.queue):
            return ui.warn("Queue Is Empty", "There is nothing in the queue.")
        emb = ui.music("Music Queue")
        if player.current is not None:
            emb.add_field(name="Now playing", value=track_link(player.current), inline=False)
        upcoming = list(player.queue)[:10]
        if upcoming:
            lines = [f"`{index}.` {track_link(track)} · {fmt_time(track.length)}" for index, track in enumerate(upcoming, start=1)]
            more = len(player.queue) - len(upcoming)
            if more > 0:
                lines.append(f"…and **{more}** more")
            emb.add_field(name="Up next", value="\n".join(lines), inline=False)
        return emb

    async def do_nowplaying(self, guild, member=None) -> discord.Embed:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player) or player.current is None:
            return ui.warn("Nothing Playing", "There is no track playing right now.")
        track = player.current
        emb = ui.music("Now Playing", track_link(track))
        emb.add_field(name="Artist", value=track.author or "Unknown", inline=True)
        position = "Live" if track.is_stream else f"{fmt_time(player.position)} / {fmt_time(track.length)}"
        emb.add_field(name="Position", value=position, inline=True)
        emb.add_field(name="Volume", value=f"{player.volume}%", inline=True)
        if track.artwork:
            emb.set_thumbnail(url=track.artwork)
        return emb

    async def do_volume(self, guild, member=None, level: Optional[int] = None) -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        if level is None:
            return ui.music("Current Volume", f"The volume is **{player.volume}%**.")
        if not 0 <= level <= 150:
            return ui.warn("Invalid Volume", "Volume must be between **0** and **150**.")
        await player.set_volume(level)
        return ui.music("Volume Updated", f"Volume set to **{level}%**.")

    async def do_loop(self, guild, member=None, mode: str = "off") -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        modes = {
            "off": (wavelink.QueueMode.normal, "Loop is **off**."),
            "track": (wavelink.QueueMode.loop, "The current track will **repeat**."),
            "queue": (wavelink.QueueMode.loop_all, "The whole queue will **repeat**."),
        }
        if mode not in modes:
            return ui.warn("Invalid Loop Mode", "Use `off`, `track` or `queue`.")
        player.queue.mode = modes[mode][0]
        return ui.music("Loop Updated", modes[mode][1])

    async def do_shuffle(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        if len(player.queue) < 2:
            return ui.warn("Not Enough Tracks", "Add at least two tracks to the queue to shuffle.")
        player.queue.shuffle()
        return ui.music("Queue Shuffled", "The queue order was randomised.")

    async def do_clear(self, guild, member=None) -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        player.queue.clear()
        return ui.music("Queue Cleared", "All upcoming tracks were removed. The current track keeps playing.")

    async def do_remove(self, guild, member=None, position: int = 0) -> discord.Embed:
        player, problem = self.get_control(guild, member, need_current=False)
        if problem is not None:
            return problem
        if not 1 <= position <= len(player.queue):
            return ui.warn("Invalid Position", "That position is not in the queue.")
        track = player.queue[position - 1]
        del player.queue[position - 1]
        return ui.music("Track Removed", f"Removed {track_link(track)} from the queue.")

    async def do_seek(self, guild, member=None, seconds: int = 0) -> discord.Embed:
        player, problem = self.get_control(guild, member)
        if problem is not None:
            return problem
        if player.current.is_stream or not player.current.is_seekable:
            return ui.warn("Cannot Seek", "This track does not support seeking.")
        target = max(0, min(seconds * 1000, player.current.length - 1000))
        await player.seek(target)
        return ui.music("Position Updated", f"Jumped to **{fmt_time(target)}**.")

    async def handle_player_button(self, interaction: discord.Interaction, action: str) -> None:
        try:
            await self._handle_player_button(interaction, action)
        except Exception as error:
            print(f"Music: panel action {action!r} failed ({type(error).__name__}: {error}).")
            embed = ui.error("Music Error", "That button could not be processed. Please try again.")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass

    async def handle_filter_select(self, interaction: discord.Interaction, value: str) -> None:
        try:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    embed=ui.warn("Server Only", "Use the music player inside a server."), ephemeral=True
                )
                return
            guild, member = interaction.guild, interaction.user
            player, problem = self.get_control(guild, member, need_current=False)
            if problem is not None:
                await interaction.response.send_message(embed=problem, ephemeral=True)
                return
            if value not in FILTER_PRESETS:
                value = "normal"
            try:
                await player.set_filters(build_filters(value))
            except Exception as error:
                print(f"Music: filter {value!r} failed ({type(error).__name__}: {error}).")
                await interaction.response.send_message(
                    embed=ui.error("Filter Failed", "The music server could not apply that filter."), ephemeral=True
                )
                return
            self._filters[guild.id] = value
            await interaction.response.edit_message(view=MusicPlayerView(self, player))
        except Exception as error:
            print(f"Music: filter menu failed ({type(error).__name__}: {error}).")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=ui.error("Music Error", "That menu could not be processed. Please try again."), ephemeral=True
                    )
            except Exception:
                pass

    async def _handle_player_button(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                embed=ui.warn("Server Only", "Use the music player inside a server."),
                ephemeral=True,
            )
            return
        guild, member = interaction.guild, interaction.user
        if action == "queue":
            player, problem = self.get_control(guild, member, need_current=False)
            if problem is not None:
                await interaction.response.send_message(embed=problem, ephemeral=True)
                return
            await interaction.response.send_message(embed=await self.do_queue(guild, member), ephemeral=True)
            return

        player, problem = self.get_control(
            guild,
            member,
            need_current=action in {"restart", "pause", "skip", "back", "forward"},
        )
        if problem is not None:
            await interaction.response.send_message(embed=problem, ephemeral=True)
            return

        if action == "restart":
            result = await self.do_seek(guild, member, 0)
        elif action == "pause":
            result = await (self.do_resume(guild, member) if player.paused else self.do_pause(guild, member))
        elif action == "skip":
            result = await self.do_skip(guild, member)
        elif action == "stop":
            result = await self.do_stop(guild, member)
        elif action == "volume_down":
            result = await self.do_volume(guild, member, max(0, player.volume - 10))
        elif action == "volume_up":
            result = await self.do_volume(guild, member, min(150, player.volume + 10))
        elif action == "back":
            result = await self.do_seek(guild, member, max(0, player.position // 1000 - 10))
        elif action == "forward":
            result = await self.do_seek(guild, member, player.position // 1000 + 10)
        elif action == "loop":
            modes = [wavelink.QueueMode.normal, wavelink.QueueMode.loop, wavelink.QueueMode.loop_all]
            mode_index = modes.index(player.queue.mode) if player.queue.mode in modes else 0
            next_mode = ("off", "track", "queue")[(mode_index + 1) % len(modes)]
            result = await self.do_loop(guild, member, next_mode)
        elif action == "shuffle":
            result = await self.do_shuffle(guild, member)
        elif action == "autoplay":
            enabled = player.autoplay is wavelink.AutoPlayMode.enabled
            player.autoplay = wavelink.AutoPlayMode.partial if enabled else wavelink.AutoPlayMode.enabled
            result = ui.music("Autoplay Updated", "Autoplay is **off**." if enabled else "Autoplay is **on**. Similar songs will play when the queue ends.")
        else:
            raise ValueError(f"Unknown music panel action: {action}")

        if ui.is_problem(result):
            await interaction.response.send_message(embed=result, ephemeral=True)
            return
        if action == "stop":
            self._player_panels.pop(guild.id, None)
            finished = MusicPlayerView(self, finished=("Playback stopped", "The queue was cleared and I left the voice channel."))
            await interaction.response.edit_message(view=finished)
            return
        await interaction.response.edit_message(view=MusicPlayerView(self, player))

    # ------------------------------------------------------------------- output
    @staticmethod
    async def send_prefix(ctx: commands.Context, emb: discord.Embed) -> None:
        await ctx.send(embed=emb)

    @staticmethod
    async def send_slash(interaction: discord.Interaction, emb: discord.Embed) -> None:
        await ui.respond(interaction, emb, ephemeral=ui.is_problem(emb))

    async def slash_run(self, interaction: discord.Interaction, coro_factory) -> None:
        """Run a music action from a slash command with a safe defer."""
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await ui.respond(interaction, ui.warn("Server Only", "Use this command inside a server."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        emb = await coro_factory(interaction.guild, interaction.user)
        await interaction.followup.send(embed=emb)

    # ------------------------------------------------------------ slash commands
    music = app_commands.Group(name="music", description="Play and control music", guild_only=True)

    @music.command(name="play", description="Play a song or playlist (name or link)")
    @app_commands.describe(query="Song name or link")
    async def s_play(self, interaction: discord.Interaction, query: str) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await ui.respond(interaction, ui.warn("Server Only", "Use this command inside a server."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await self.do_play(interaction.guild, interaction.user, query)
        if ui.is_problem(result):
            await interaction.followup.send(embed=result, ephemeral=True)
            return

        async def create_panel() -> discord.Message:
            player = interaction.guild.voice_client
            if not isinstance(player, wavelink.Player) or player.current is None:
                return await interaction.followup.send(embed=result, wait=True)
            return await interaction.followup.send(
                view=MusicPlayerView(self, player),
                wait=True,
            )

        created = await self.upsert_player_panel(interaction.guild, create_panel)
        if not created:
            await interaction.followup.send(embed=result, ephemeral=True)

    @music.command(name="pause", description="Pause the music")
    async def s_pause(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_pause)

    @music.command(name="resume", description="Resume the music")
    async def s_resume(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_resume)

    @music.command(name="skip", description="Skip the current track")
    async def s_skip(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_skip)

    @music.command(name="stop", description="Stop the music and leave the voice channel")
    async def s_stop(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_stop)
        if interaction.guild is not None:
            await self.finish_player_panel(interaction.guild.id)

    @music.command(name="queue", description="Show the queue")
    async def s_queue(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_queue)

    @music.command(name="nowplaying", description="Show the current track")
    async def s_nowplaying(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_nowplaying)

    @music.command(name="volume", description="Show or change the volume (0-150)")
    async def s_volume(self, interaction: discord.Interaction, level: Optional[app_commands.Range[int, 0, 150]] = None) -> None:
        await self.slash_run(interaction, lambda g, m: self.do_volume(g, m, level))

    @music.command(name="loop", description="Set loop mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Current track", value="track"),
            app_commands.Choice(name="Whole queue", value="queue"),
        ]
    )
    async def s_loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self.slash_run(interaction, lambda g, m: self.do_loop(g, m, mode.value))

    @music.command(name="shuffle", description="Shuffle the queue")
    async def s_shuffle(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_shuffle)

    @music.command(name="clear", description="Clear the queue")
    async def s_clear(self, interaction: discord.Interaction) -> None:
        await self.slash_run(interaction, self.do_clear)

    @music.command(name="remove", description="Remove a track from the queue")
    async def s_remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]) -> None:
        await self.slash_run(interaction, lambda g, m: self.do_remove(g, m, position))

    @music.command(name="seek", description="Jump to a position in the current track (seconds)")
    async def s_seek(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 86400]) -> None:
        await self.slash_run(interaction, lambda g, m: self.do_seek(g, m, seconds))

    # ------------------------------------------------------------- dot commands
    @commands.command(name="play", aliases=["p"], brief="Play a song or playlist.", extras={"category": "Music", "usage": ".play <song or link>"})
    @commands.guild_only()
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def p_play(self, ctx: commands.Context, *, query: str) -> None:
        async with ctx.typing():
            result = await self.do_play(ctx.guild, ctx.author, query)
        if ui.is_problem(result):
            await self.send_prefix(ctx, result)
            return

        async def create_panel() -> discord.Message:
            player = ctx.guild.voice_client
            if not isinstance(player, wavelink.Player) or player.current is None:
                return await ctx.send(embed=result)
            return await ctx.send(view=MusicPlayerView(self, player))

        created = await self.upsert_player_panel(ctx.guild, create_panel)
        if not created:
            await self.send_prefix(ctx, result)

    @commands.command(name="pause", brief="Pause the music.", extras={"category": "Music", "usage": ".pause"})
    @commands.guild_only()
    async def p_pause(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_pause(ctx.guild, ctx.author))

    @commands.command(name="resume", brief="Resume the music.", extras={"category": "Music", "usage": ".resume"})
    @commands.guild_only()
    async def p_resume(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_resume(ctx.guild, ctx.author))

    @commands.command(name="skip", aliases=["s"], brief="Skip the current track.", extras={"category": "Music", "usage": ".skip"})
    @commands.guild_only()
    async def p_skip(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_skip(ctx.guild, ctx.author))

    @commands.command(name="stop", aliases=["dc"], brief="Stop the music and leave.", extras={"category": "Music", "usage": ".stop"})
    @commands.guild_only()
    async def p_stop(self, ctx: commands.Context) -> None:
        result = await self.do_stop(ctx.guild, ctx.author)
        await self.finish_player_panel(ctx.guild.id)
        await self.send_prefix(ctx, result)

    @commands.command(name="queue", aliases=["q"], brief="Show the queue.", extras={"category": "Music", "usage": ".queue"})
    @commands.guild_only()
    async def p_queue(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_queue(ctx.guild, ctx.author))

    @commands.command(name="np", aliases=["nowplaying"], brief="Show the current track.", extras={"category": "Music", "usage": ".np"})
    @commands.guild_only()
    async def p_np(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_nowplaying(ctx.guild, ctx.author))

    @commands.command(name="volume", aliases=["vol"], brief="Show or set the volume (0-150).", extras={"category": "Music", "usage": ".volume [0-150]"})
    @commands.guild_only()
    async def p_volume(self, ctx: commands.Context, level: Optional[int] = None) -> None:
        await self.send_prefix(ctx, await self.do_volume(ctx.guild, ctx.author, level))

    @commands.command(name="loop", brief="Set loop mode: off, track or queue.", extras={"category": "Music", "usage": ".loop off|track|queue"})
    @commands.guild_only()
    async def p_loop(self, ctx: commands.Context, mode: str = "track") -> None:
        await self.send_prefix(ctx, await self.do_loop(ctx.guild, ctx.author, mode.lower()))

    @commands.command(name="shuffle", brief="Shuffle the queue.", extras={"category": "Music", "usage": ".shuffle"})
    @commands.guild_only()
    async def p_shuffle(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_shuffle(ctx.guild, ctx.author))

    @commands.command(name="clearqueue", aliases=["cq"], brief="Clear the queue.", extras={"category": "Music", "usage": ".clearqueue"})
    @commands.guild_only()
    async def p_clear(self, ctx: commands.Context) -> None:
        await self.send_prefix(ctx, await self.do_clear(ctx.guild, ctx.author))

    @commands.command(name="remove", brief="Remove a track from the queue.", extras={"category": "Music", "usage": ".remove <position>"})
    @commands.guild_only()
    async def p_remove(self, ctx: commands.Context, position: int) -> None:
        await self.send_prefix(ctx, await self.do_remove(ctx.guild, ctx.author, position))

    @commands.command(name="seek", brief="Jump to a position (seconds).", extras={"category": "Music", "usage": ".seek <seconds>"})
    @commands.guild_only()
    async def p_seek(self, ctx: commands.Context, seconds: int) -> None:
        await self.send_prefix(ctx, await self.do_seek(ctx.guild, ctx.author, seconds))


class MusicPlayerView(discord.ui.LayoutView):
    """Premium now-playing card (Components V2). Pass `finished=(title, text)` for the closed state."""

    def __init__(self, cog: MusicCog, player: Optional[wavelink.Player] = None, finished: Optional[tuple[str, str]] = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        try:
            container = self._build(player, finished)
        except Exception as error:
            print(f"Music: could not build the player card ({type(error).__name__}: {error}).")
            container = discord.ui.Container(
                discord.ui.TextDisplay("-# KRYO PRIME  •  MUSIC\nPlayer is loading, press a button to refresh."),
                accent_colour=discord.Colour(ACCENT),
            )
        self.add_item(container)

    # -- helpers
    def _button(self, action: str, emoji: Optional[str] = None, label: Optional[str] = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
        button = discord.ui.Button(emoji=emoji, label=label, style=style, custom_id=f"kryo:music:{action}")
        cog = self.cog

        async def callback(interaction: discord.Interaction) -> None:
            await cog.handle_player_button(interaction, action)

        button.callback = callback
        return button

    def _filter_select(self, current: str) -> discord.ui.Select:
        select = discord.ui.Select(
            custom_id="kryo:music:filter",
            placeholder="Audio filter",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, description=description, default=(key == current))
                for key, (label, description) in FILTER_PRESETS.items()
            ],
        )
        cog = self.cog

        async def callback(interaction: discord.Interaction) -> None:
            value = select.values[0] if select.values else "normal"
            await cog.handle_filter_select(interaction, value)

        select.callback = callback
        return select

    def _build(self, player: Optional[wavelink.Player], finished: Optional[tuple[str, str]]) -> discord.ui.Container:
        container = discord.ui.Container(accent_colour=discord.Colour(ACCENT))
        if finished is not None:
            title, text = finished
            container.add_item(discord.ui.TextDisplay(f"-# KRYO PRIME  •  MUSIC\n## {title}\n{text}"))
            return container

        track = player.current if player is not None else None
        guild_id = player.guild.id if player is not None and player.guild is not None else 0
        current_filter = self.cog._filters.get(guild_id, "normal")
        autoplay_on = False
        loop_mode = wavelink.QueueMode.normal

        if track is None or player is None:
            container.add_item(discord.ui.TextDisplay("-# KRYO PRIME  •  MUSIC\n## Nothing is playing\nUse `/music play` to start."))
        else:
            autoplay_on = player.autoplay is wavelink.AutoPlayMode.enabled
            loop_mode = player.queue.mode
            state = "PAUSED" if player.paused else "NOW PLAYING"
            title = clean_text(track.title or "Unknown title")
            heading = f"## [{title}]({track.uri})" if track.uri else f"## {title}"
            requester = self.cog._requesters.get(track.encoded)
            ping = getattr(player, "ping", None)
            info = []
            if player.channel is not None:
                info.append(f"🔊 {player.channel.mention}")
            if requester:
                info.append(f"👤 {clean_text(requester, 32)}")
            if isinstance(ping, int) and ping >= 0:
                info.append(f"📶 {ping}ms")
            lines = f"{heading}\n{clean_text(track.author or 'Unknown artist', 60)}"
            if info:
                lines += "\n-# " + "  •  ".join(info)
            header = discord.ui.TextDisplay(f"-# KRYO PRIME  •  {state}")
            body = discord.ui.TextDisplay(lines)
            container.add_item(header)
            artwork = track.artwork if isinstance(track.artwork, str) and track.artwork.startswith("http") else None
            if artwork:
                container.add_item(discord.ui.Section(body, accessory=discord.ui.Thumbnail(artwork)))
            else:
                container.add_item(body)

            container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
            wave = waveform(track.identifier or track.title or "")
            if track.is_stream or track.length <= 0:
                timeline = f"{wave}\n`LIVE`"
            else:
                position = max(0, min(int(player.position), track.length))
                timeline = f"{wave}\n`{fmt_time(position)}`  {progress_bar(position, track.length)}  `{fmt_time(track.length)}`"
            container.add_item(discord.ui.TextDisplay(timeline))
            loop_names = {
                wavelink.QueueMode.normal: "Off",
                wavelink.QueueMode.loop: "Track",
                wavelink.QueueMode.loop_all: "Queue",
            }
            stats = (
                f"-# Volume {player.volume}%  •  Loop {loop_names.get(loop_mode, 'Off')}  •  "
                f"Autoplay {'On' if autoplay_on else 'Off'}  •  Queue {len(player.queue)}  •  "
                f"Filter {FILTER_PRESETS.get(current_filter, FILTER_PRESETS['normal'])[0]}"
            )
            container.add_item(discord.ui.TextDisplay(stats))

        container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        secondary, primary, success, danger = (
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.primary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
        )
        container.add_item(
            discord.ui.ActionRow(
                self._button("shuffle", emoji="🔀"),
                self._button("restart", emoji="⏮️"),
                self._button("pause", emoji="⏯️", style=primary),
                self._button("skip", emoji="⏭️"),
                self._button("loop", emoji="🔁", style=success if loop_mode is not wavelink.QueueMode.normal else secondary),
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                self._button("volume_down", emoji="🔉"),
                self._button("volume_up", emoji="🔊"),
                self._button("back", emoji="⏪"),
                self._button("forward", emoji="⏩"),
                self._button("autoplay", emoji="♾️", style=success if autoplay_on else secondary),
            )
        )
        container.add_item(discord.ui.ActionRow(self._filter_select(current_filter)))
        container.add_item(
            discord.ui.ActionRow(
                self._button("queue", emoji="📜", label="Queue"),
                self._button("stop", emoji="⏹️", label="Leave", style=danger),
            )
        )
        return container
