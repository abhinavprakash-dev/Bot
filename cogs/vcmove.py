import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui
from botcore.perms import member_problem


async def _move_members_in_batches(
    members: list[discord.Member],
    destination: discord.VoiceChannel,
    reason: str,
) -> tuple[int, int]:
    async def move_member(member: discord.Member) -> bool:
        try:
            await member.move_to(destination, reason=reason)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    moved = 0
    batch_size = 5
    for start in range(0, len(members), batch_size):
        batch = members[start : start + batch_size]
        results = await asyncio.gather(*(move_member(member) for member in batch))
        moved += sum(results)

    return moved, len(members) - moved


def has_move_members_prefix():
    """Check for Move Members permission (not full administrator)."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not isinstance(ctx.author, discord.Member):
            raise commands.MissingPermissions(["move_members"])
        if not ctx.author.guild_permissions.move_members:
            raise commands.MissingPermissions(["move_members"])
        return True
    return commands.check(predicate)


def has_move_members_slash():
    """Check for Move Members permission (not full administrator)."""
    return app_commands.checks.has_permissions(move_members=True)


async def _can_move(
    ctx: commands.Context,
    member: Optional[discord.Member] = None,
    channel: Optional[discord.VoiceChannel] = None,
) -> Optional[discord.Embed]:
    guild = ctx.guild
    if guild is None or not isinstance(ctx.author, discord.Member):
        return ui.warn("Server Only", "This command can only be used inside a server.")
    me = guild.me
    if me is None:
        return ui.error("Not Ready", "The bot is not ready yet.")
    if not ctx.author.guild_permissions.move_members:
        return ui.error("Missing Permission", "You need the **Move Members** permission to use this command.")
    if not me.guild_permissions.move_members:
        return ui.error("Missing Permission", "I need the **Move Members** permission to move members.")
    if member is not None:
        problem = member_problem(guild, ctx.author, member)
        if problem:
            return ui.error("Action Blocked", problem)
        if member.bot:
            return ui.warn("Bots Skipped", "Bots cannot be moved with this command.")
    if channel is not None and not isinstance(channel, discord.VoiceChannel):
        return ui.warn("Invalid Channel", "Please specify a voice channel.")
    return None


async def _can_move_slash(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    channel: Optional[discord.VoiceChannel] = None,
) -> Optional[discord.Embed]:
    guild = interaction.guild
    actor = interaction.user
    if guild is None or not isinstance(actor, discord.Member):
        return ui.warn("Server Only", "This command can only be used inside a server.")
    me = guild.me
    if me is None:
        return ui.error("Not Ready", "The bot is not ready yet.")
    if not actor.guild_permissions.move_members:
        return ui.error("Missing Permission", "You need the **Move Members** permission to use this command.")
    if not me.guild_permissions.move_members:
        return ui.error("Missing Permission", "I need the **Move Members** permission to move members.")
    if member is not None:
        problem = member_problem(guild, actor, member)
        if problem:
            return ui.error("Action Blocked", problem)
        if member.bot:
            return ui.warn("Bots Skipped", "Bots cannot be moved with this command.")
    if channel is not None and not isinstance(channel, discord.VoiceChannel):
        return ui.warn("Invalid Channel", "Please specify a voice channel.")
    return None


class VCMoveCog(commands.Cog, name="VC Move"):
    """Enhanced voice channel move commands with reply support, batch moving, and anti-nuke bypass."""

    drag = app_commands.Group(name="drag", description="Move voice channel members")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _audit_reason(actor: discord.abc.User) -> str:
        return f"Moved by {actor} ({actor.id})"

    async def _moveall_slash(
        self,
        interaction: discord.Interaction,
        dest: discord.VoiceChannel,
        source: Optional[discord.VoiceChannel],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if source is None:
            user = interaction.user
            voice_state = user.voice if isinstance(user, discord.Member) else None
            voice_channel = voice_state.channel if voice_state is not None else None
            if isinstance(voice_channel, discord.VoiceChannel):
                source = voice_channel
            else:
                await ui.respond(interaction, ui.warn("No Source Channel", "Join a voice channel or specify the source channel."), ephemeral=True)
                return

        if not isinstance(source, discord.VoiceChannel) or not isinstance(dest, discord.VoiceChannel):
            await ui.respond(interaction, ui.warn("Invalid Channel", "Please specify voice source and destination channels."), ephemeral=True)
            return
        if source.id == dest.id:
            await ui.respond(interaction, ui.warn("Same Channel", "Source and destination channels cannot be the same."), ephemeral=True)
            return

        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            await ui.respond(interaction, ui.warn("Server Only", "This command can only be used inside a server."), ephemeral=True)
            return
        me = guild.me
        if me is None:
            await ui.respond(interaction, ui.error("Not Ready", "The bot is not ready yet."), ephemeral=True)
            return
        if not actor.guild_permissions.move_members:
            await ui.respond(interaction, ui.error("Missing Permission", "You need the **Move Members** permission to use this command."), ephemeral=True)
            return
        if not me.guild_permissions.move_members:
            await ui.respond(interaction, ui.error("Missing Permission", "I need the **Move Members** permission to move members."), ephemeral=True)
            return

        members_to_move = [member for member in source.members if not member.bot]
        if not members_to_move:
            await ui.respond(interaction, ui.info("No Members", f"No non-bot members in {source.mention}."), ephemeral=True)
            return

        reason = self._audit_reason(actor)
        moved, failed = await _move_members_in_batches(members_to_move, dest, reason)

        embed = discord.Embed(
            title="Move All Complete",
            description=f"Moved members from {source.mention} to {dest.mention}",
            color=discord.Color.green() if failed == 0 else discord.Color.orange(),
        )
        embed.add_field(name="Moved", value=str(moved), inline=True)
        embed.add_field(name="Failed", value=str(failed), inline=True)
        embed.add_field(name="Skipped (Bots)", value=str(len(source.members) - len(members_to_move)), inline=True)
        await ui.respond(interaction, embed, ephemeral=False)

    # ------------------------------------------------------------------ .join
    @commands.command(
        name="join",
        brief="Make the bot join your current voice channel",
        extras={"category": "Voice", "usage": ".join"},
    )
    @commands.guild_only()
    async def join_prefix(self, ctx: commands.Context) -> None:
        author = ctx.author
        if not isinstance(author, discord.Member) or author.voice is None:
            await ctx.send(embed=ui.warn("Not In Voice", "Join a voice channel first."))
            return

        channel = author.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.send(embed=ui.warn("Not In Voice", "Join a voice channel first."))
            return
        try:
            await channel.connect()
            await ctx.send(embed=ui.success("Joined Voice", f"Connected to {channel.mention}."))
        except discord.Forbidden:
            await ctx.send(embed=ui.error("Missing Permission", "I need **Connect** permission to join this voice channel."))
        except discord.ClientException:
            await ctx.send(embed=ui.warn("Already Connected", "I am already connected to another voice channel."))
        except Exception as e:
            await ctx.send(embed=ui.error("Failed to Join", f"{e}"))

    @app_commands.command(name="join", description="Make the bot join your current voice channel")
    @app_commands.guild_only()
    async def join_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        if not isinstance(user, discord.Member) or user.voice is None:
            await ui.respond(interaction, ui.warn("Not In Voice", "Join a voice channel first."), ephemeral=True)
            return

        channel = user.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await ui.respond(interaction, ui.warn("Not In Voice", "Join a voice channel first."), ephemeral=True)
            return
        try:
            await channel.connect()
            await ui.respond(interaction, ui.success("Joined Voice", f"Connected to {channel.mention}."), ephemeral=False)
        except discord.Forbidden:
            await ui.respond(interaction, ui.error("Missing Permission", "I need **Connect** permission to join this voice channel."), ephemeral=True)
        except discord.ClientException:
            await ui.respond(interaction, ui.warn("Already Connected", "I am already connected to another voice channel."), ephemeral=True)
        except Exception as e:
            await ui.respond(interaction, ui.error("Failed to Join", f"{e}"), ephemeral=True)

    # ------------------------------------------------------------------ .leave
    @commands.command(
        name="leave",
        brief="Make the bot leave the current voice channel",
        extras={"category": "Voice", "usage": ".leave"},
    )
    @commands.guild_only()
    async def leave_prefix(self, ctx: commands.Context) -> None:
        if not ctx.voice_client:
            await ctx.send(embed=ui.warn("Not Connected", "I am not connected to any voice channel."))
            return

        channel = ctx.voice_client.channel
        await ctx.voice_client.disconnect(force=True)
        channel_mention = (
            channel.mention
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            else "the voice channel"
        )
        await ctx.send(embed=ui.success("Disconnected", f"Disconnected from {channel_mention}."))

    @app_commands.command(name="leave", description="Make the bot leave the current voice channel")
    @app_commands.guild_only()
    async def leave_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        voice_client = guild.voice_client if guild is not None else None
        if voice_client is None:
            await ui.respond(interaction, ui.warn("Not Connected", "I am not connected to any voice channel."), ephemeral=True)
            return

        channel = voice_client.channel
        await voice_client.disconnect(force=True)
        channel_mention = (
            channel.mention
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            else "the voice channel"
        )
        await ui.respond(interaction, ui.success("Disconnected", f"Disconnected from {channel_mention}."), ephemeral=False)

    # ------------------------------------------------------------------ .move
    @commands.command(
        name="move",
        brief="Move a member to your VC: .move [@user] [#vc] (reply to a message to move that author)",
        extras={"category": "Voice", "usage": ".move [@user] [#voice]"},
    )
    @commands.guild_only()
    @has_move_members_prefix()
    async def move_prefix(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        """Move a member to a voice channel.

        - Reply to a message with `.move` to move that message's author to your current VC.
        - `.move @user` moves the user to your current VC.
        - `.move @user #vc` moves the user to the specified VC.
        """
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used inside a server."))
            return

        # Determine target member from reply
        reference = ctx.message.reference
        if member is None and reference is not None and reference.message_id is not None:
            try:
                ref = await ctx.channel.fetch_message(reference.message_id)
                author = ref.author
                member = author if isinstance(author, discord.Member) else guild.get_member(author.id)
            except (discord.NotFound, discord.HTTPException, AttributeError):
                pass

        if member is None:
            await ctx.send(embed=ui.warn("No Member", "Specify a member or reply to a message from a server member."))
            return

        # Determine target voice channel
        if channel is None:
            author = ctx.author
            voice_state = author.voice if isinstance(author, discord.Member) else None
            voice_channel = voice_state.channel if voice_state is not None else None
            if isinstance(voice_channel, discord.VoiceChannel):
                channel = voice_channel
            else:
                await ctx.send(embed=ui.warn("No Voice Channel", "Join a voice channel or specify one."))
                return

        # Permission checks
        error = await _can_move(ctx, member, channel)
        if error:
            await ctx.send(embed=error)
            return

        # Check if member is in a voice channel
        if member.voice is None or member.voice.channel is None:
            await ctx.send(embed=ui.warn("Not In Voice", f"{member.mention} is not in a voice channel."))
            return

        # Check if already in target channel
        if member.voice.channel.id == channel.id:
            await ctx.send(embed=ui.warn("Already There", f"{member.mention} is already in {channel.mention}."))
            return

        # Perform the move
        try:
            await member.move_to(channel, reason=self._audit_reason(ctx.author))
        except discord.Forbidden:
            await ctx.send(embed=ui.error("Missing Permissions", "I cannot move that member (role hierarchy or missing permission)."))
            return
        except discord.HTTPException as e:
            await ctx.send(embed=ui.error("Move Failed", f"Failed to move member: {e}"))
            return

        await ctx.send(embed=ui.success("Member Moved", f"{member.mention} was moved to {channel.mention}."))

    @app_commands.command(name="move", description="Move a member to a voice channel")
    @app_commands.describe(
        member="Member to move (optional: reply to a message instead)",
        channel="Destination voice channel (default: your current VC)",
    )
    @app_commands.guild_only()
    @has_move_members_slash()
    async def move_slash(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if member is None:
            await ui.respond(interaction, ui.warn("No Member", "Specify a member or reply to a message with the prefix command."), ephemeral=True)
            return

        if channel is None:
            user = interaction.user
            voice_state = user.voice if isinstance(user, discord.Member) else None
            voice_channel = voice_state.channel if voice_state is not None else None
            if isinstance(voice_channel, discord.VoiceChannel):
                channel = voice_channel
            else:
                await ui.respond(interaction, ui.warn("No Voice Channel", "Join a voice channel or specify one."), ephemeral=True)
                return

        error = await _can_move_slash(interaction, member, channel)
        if error:
            await ui.respond(interaction, error, ephemeral=True)
            return

        if member.voice is None or member.voice.channel is None:
            await ui.respond(interaction, ui.warn("Not In Voice", f"{member.mention} is not in a voice channel."), ephemeral=True)
            return

        if member.voice.channel.id == channel.id:
            await ui.respond(interaction, ui.warn("Already There", f"{member.mention} is already in {channel.mention}."), ephemeral=True)
            return

        try:
            await member.move_to(channel, reason=self._audit_reason(interaction.user))
        except discord.Forbidden:
            await ui.respond(interaction, ui.error("Missing Permissions", "I cannot move that member (role hierarchy or missing permission)."), ephemeral=True)
            return
        except discord.HTTPException as e:
            await ui.respond(interaction, ui.error("Move Failed", f"Failed to move member: {e}"), ephemeral=True)
            return

        await ui.respond(interaction, ui.success("Member Moved", f"{member.mention} was moved to {channel.mention}."), ephemeral=False)

    # ------------------------------------------------------------------ .moveall
    @commands.command(
        name="moveall",
        aliases=["moveallvc", "dragall"],
        brief="Move all members: .moveall #dest (from your VC) or .moveall #src #dest",
        extras={"category": "Voice", "usage": ".moveall #dest | .moveall #src #dest"},
    )
    @commands.guild_only()
    @has_move_members_prefix()
    async def moveall_prefix(
        self,
        ctx: commands.Context,
        source: Optional[discord.VoiceChannel] = None,
        dest: Optional[discord.VoiceChannel] = None,
    ) -> None:
        """Move all members from one voice channel to another.

        - `.moveall #dest` — moves everyone from your current VC to #dest.
        - `.moveall #src #dest` — moves everyone from #src to #dest.
        - Skips bots to avoid breaking music players.
        - Moves up to five members concurrently; Discord API rate limits are handled by the client.
        """
        guild = ctx.guild
        actor = ctx.author
        if guild is None or not isinstance(actor, discord.Member):
            await ctx.send(embed=ui.warn("Server Only", "This command can only be used inside a server."))
            return

        # Determine source channel
        if source is None and dest is None:
            await ctx.send(embed=ui.warn("Usage", "Use `.moveall #dest` or `.moveall #src #dest`."))
            return

        if dest is None:
            # Only one channel provided: treat as destination, source is author's VC
            if source is None:
                await ctx.send(embed=ui.warn("Usage", "Use `.moveall #dest` or `.moveall #src #dest`."))
                return
            dest = source
            voice_state = actor.voice
            voice_channel = voice_state.channel if voice_state is not None else None
            if isinstance(voice_channel, discord.VoiceChannel):
                source = voice_channel
            else:
                await ctx.send(embed=ui.warn("No Source Channel", "Join a voice channel or specify the source channel."))
                return

        if source is None or dest is None:
            await ctx.send(embed=ui.warn("Invalid Channel", "Please specify voice source and destination channels."))
            return
        if not isinstance(source, discord.VoiceChannel) or not isinstance(dest, discord.VoiceChannel):
            await ctx.send(embed=ui.warn("Invalid Channel", "Please specify voice source and destination channels."))
            return

        if source.id == dest.id:
            await ctx.send(embed=ui.warn("Same Channel", "Source and destination channels cannot be the same."))
            return

        # Permission checks
        me = guild.me
        if me is None:
            await ctx.send(embed=ui.error("Not Ready", "The bot is not ready yet."))
            return
        if not actor.guild_permissions.move_members:
            await ctx.send(embed=ui.error("Missing Permission", "You need the **Move Members** permission to use this command."))
            return
        if not me.guild_permissions.move_members:
            await ctx.send(embed=ui.error("Missing Permission", "I need the **Move Members** permission to move members."))
            return

        # Get members to move (skip bots)
        members_to_move = [m for m in source.members if not m.bot]
        if not members_to_move:
            await ctx.send(embed=ui.info("No Members", f"No non-bot members in {source.mention}."))
            return

        reason = self._audit_reason(actor)
        moved, failed = await _move_members_in_batches(members_to_move, dest, reason)

        embed = discord.Embed(
            title="Move All Complete",
            description=f"Moved members from {source.mention} to {dest.mention}",
            color=discord.Color.green() if failed == 0 else discord.Color.orange(),
        )
        embed.add_field(name="Moved", value=str(moved), inline=True)
        embed.add_field(name="Failed", value=str(failed), inline=True)
        embed.add_field(name="Skipped (Bots)", value=str(len(source.members) - len(members_to_move)), inline=True)
        await ctx.send(embed=embed)

    @app_commands.command(name="moveall", description="Move all members from one VC to another")
    @app_commands.describe(
        source="Source voice channel (default: your current VC)",
        dest="Destination voice channel",
    )
    @app_commands.guild_only()
    @has_move_members_slash()
    async def moveall_slash(
        self,
        interaction: discord.Interaction,
        dest: discord.VoiceChannel,
        source: Optional[discord.VoiceChannel] = None,
    ) -> None:
        await self._moveall_slash(interaction, dest, source)

    @drag.command(name="all", description="Move all members from one voice channel to another")
    @app_commands.describe(
        dest="Destination voice channel",
        source="Source voice channel (default: your current VC)",
    )
    @app_commands.guild_only()
    @has_move_members_slash()
    async def drag_all_slash(
        self,
        interaction: discord.Interaction,
        dest: discord.VoiceChannel,
        source: Optional[discord.VoiceChannel] = None,
    ) -> None:
        await self._moveall_slash(interaction, dest, source)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VCMoveCog(bot))