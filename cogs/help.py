import os
import traceback
from typing import Optional

import discord
from discord.ext import commands

from botcore import kryo_emojis as prime_emojis, ui, log_exception

MAIN_MENU = ["Anti Nuke", "Auto Mod", "Role Config", "Moderation", "Smart Moderation", "Join To Create", "No Prefix"]
OTHERS_MENU = ["Other", "Auto Role", "Welcomer", "Self Roles", "Utility", "Voice", "Music"]
ALL_CATEGORIES = MAIN_MENU + OTHERS_MENU

# label -> (cog name, method) used by the "Show Automatic Features" button
AUTOMATIC = {
    "Anti Nuke": ("Security", "nuke_active"),
    "Auto Mod": ("Security", "automod_on"),
    "Smart Moderation": ("Smart Moderation", "feature_on"),
    "Join To Create": ("Join To Create", "feature_on"),
    "Auto Role": ("Auto Role", "feature_on"),
    "Welcomer": ("Welcomer", "feature_on"),
    "Server Logs": ("Logs", "active"),
}


# ----------------------------------------------------------------- command helpers
def check_names(cmd: commands.Command) -> list[str]:
    return [getattr(check, "__qualname__", "") for check in cmd.checks]


def owner_only(cmd: commands.Command) -> bool:
    return any("is_owner" in name for name in check_names(cmd))


def admin_only(cmd: commands.Command) -> bool:
    markers = ("has_guild_permissions", "has_permissions", "has_role", "has_any_role")
    return any(marker in name for name in check_names(cmd) for marker in markers)


def category_of(cmd: commands.Command) -> str:
    current: Optional[commands.Command] = cmd
    while current is not None:
        category = current.extras.get("category")
        if category in ALL_CATEGORIES:
            return category
        if current.cog_name in ALL_CATEGORIES:
            return current.cog_name
        current = current.parent
    return "Other"


def visible(cmd: commands.Command, level: int) -> bool:
    """level 0 = member, 1 = administrator, 2 = bot owner."""
    if cmd.hidden:
        return False
    if level >= 2:
        return True
    if owner_only(cmd):
        return False
    if level == 1:
        return True
    return not admin_only(cmd)


def usage_of(cmd: commands.Command) -> str:
    usage = cmd.extras.get("usage")
    if usage:
        return usage
    signature = cmd.signature
    return f".{cmd.qualified_name}" + (f" {signature}" if signature else "")


def short(text: Optional[str], limit: int = 70) -> str:
    if not text:
        return ""
    first = text.strip().splitlines()[0]
    return first if len(first) <= limit else first[: limit - 1] + "…"


def visible_commands(cmd: commands.Command, level: int) -> list[commands.Command]:
    """The command itself plus every visible subcommand, ready for display."""
    found: list[commands.Command] = []
    if visible(cmd, level):
        found.append(cmd)
    if isinstance(cmd, commands.Group):
        found.extend(sub for sub in sorted(cmd.commands, key=lambda c: c.name) if visible(sub, level))
    return found


def collect(bot: commands.Bot, level: int) -> dict[str, list[commands.Command]]:
    buckets: dict[str, list[commands.Command]] = {}
    for cmd in sorted(bot.commands, key=lambda c: c.name):
        found = visible_commands(cmd, level)
        if found:
            buckets.setdefault(category_of(cmd), []).extend(found)
    return buckets


# ---------------------------------------------------------------------- embeds
PAGE_SIZE = 10

# Display-only grouping for the home screen. The dropdowns keep using the two menus above.
SECTIONS: list[tuple[str, list[str]]] = [
    ("SECURITY", ["Anti Nuke", "Auto Mod", "Smart Moderation"]),
    ("SERVER SETUP", ["Role Config", "Voice", "Join To Create", "No Prefix"]),
    ("COMMUNITY", ["Welcomer", "Auto Role", "Self Roles", "Music", "Utility", "Other"]),
]


def page_count(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def command_count(count: int) -> str:
    return f"{count} command" if count == 1 else f"{count} commands"


def command_line(cmd: commands.Command) -> str:
    """One command on its own line, its description on the next one."""
    line = f"{ui.SPIDER} `{usage_of(cmd)}`"
    body = short(cmd.brief or cmd.help, 100)
    return f"{line}\n> {body}" if body else line


def automatic_embed(bot: commands.Bot, guild: discord.Guild) -> discord.Embed:
    rows = []
    active = 0
    for label, (cog_name, method) in AUTOMATIC.items():
        cog = bot.get_cog(cog_name)
        state = False
        if cog is not None:
            try:
                state = bool(getattr(cog, method)(guild))
            except Exception:
                state = False
        if state:
            active += 1
        rows.append(f"{'🟢' if state else '⚪'} **{label}**")
    emb = discord.Embed(
        title=f"{ui.SPIDER} AUTOMATIC FEATURES",
        description=(
            "These protections work on their own — nobody has to type a command.\n\n"
            + "\n".join(rows)
            + f"\n\n**Active here: {active} of {len(AUTOMATIC)}**"
            + "\n\n> Turn them on from their category in the `.help` menu. Most need an active **PRIME** plan."
        ),
        color=0x8A2BE2,
        timestamp=discord.utils.utcnow(),
    )
    emb.set_footer(text=f"{ui.BRAND} • Automatic Features")
    if bot.user is not None:
        emb.set_thumbnail(url=bot.user.display_avatar.url)
    return emb


def detail_embed(cmd: commands.Command) -> discord.Embed:
    access = "Bot owner" if owner_only(cmd) else ("Administrators" if admin_only(cmd) else "Everyone")
    emb = discord.Embed(
        title=f"{ui.SPIDER} .{cmd.qualified_name}",
        description=cmd.brief or cmd.help or "No description.",
        color=0x8A2BE2,
        timestamp=discord.utils.utcnow(),
    )
    emb.add_field(name="Usage", value=f"`{usage_of(cmd)}`", inline=True)
    emb.add_field(name="Category", value=category_of(cmd), inline=True)
    emb.add_field(name="Who can use it", value=access, inline=True)
    if cmd.aliases:
        emb.add_field(name="Aliases", value=" ".join(f"`.{alias}`" for alias in cmd.aliases)[:1024], inline=False)
    if isinstance(cmd, commands.Group) and cmd.commands:
        names = " ".join(f"`{sub.name}`" for sub in sorted(cmd.commands, key=lambda c: c.name))
        emb.add_field(name=f"Subcommands ({len(cmd.commands)})", value=names[:1024], inline=False)
    emb.set_footer(text=f"{ui.BRAND} • .help to go back to the menu")
    return emb


# ------------------------------------------------------------------------ views (Components V2)
ACCENT = discord.Colour(0x8A2BE2)


def chip(name: str, count: int) -> str:
    label = f"{name} {count}".replace(" ", "\u00a0")
    return f"{prime_emojis.icon(name)}\u00a0`{label}`"


def home_items(bot: commands.Bot, guild: discord.Guild, buckets: dict[str, list[commands.Command]]) -> list:
    security = bot.get_cog("Security")
    antinuke_on = security is not None and security.nuke_active(guild)
    total = sum(len(items) for items in buckets.values())

    header = discord.ui.TextDisplay(
        f"## {prime_emojis.icon('Brand')} {ui.BRAND} Command Center\n"
        "Advanced security, moderation and music in one bot. "
        "Pick a category from the menu below or use `.help <command>`"
    )
    items: list = []
    if bot.user is not None:
        items.append(discord.ui.Section(header, accessory=discord.ui.Thumbnail(bot.user.display_avatar.url)))
    else:
        items.append(header)
    status = "🟢\u00a0**Active**" if antinuke_on else "🔴\u00a0**Disabled**"
    items.append(discord.ui.TextDisplay(
        f"`Prefix`\u00a0**.**   `Commands`\u00a0**{total}**\n`Antinuke`\u00a0{status}"
    ))
    items.append(discord.ui.Separator())
    for title, names in SECTIONS:
        chips = "   ".join(chip(entry, len(buckets[entry])) for entry in names if entry in buckets)
        if chips:
            items.append(discord.ui.TextDisplay(f"**{title}**\n{chips}"))
    return items


def category_items(name: str, cmds: list[commands.Command], page: int) -> list:
    pages = page_count(len(cmds))
    page = max(0, min(pages - 1, page))
    start = page * PAGE_SIZE
    window = cmds[start : start + PAGE_SIZE]
    items: list = [
        discord.ui.TextDisplay(f"## {prime_emojis.icon(name)} {name}\n-# {command_count(len(cmds))} • page {page + 1} of {pages}"),
        discord.ui.Separator(),
    ]
    if window:
        body = "\n\n".join(command_line(cmd) for cmd in window)
    else:
        body = "*Nothing available for you here.*"
    items.append(discord.ui.TextDisplay(body[:3500]))
    return items


class CategorySelect(discord.ui.Select):
    def __init__(self, help_view: "HelpView", placeholder: str, names: list[str]) -> None:
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                emoji=prime_emojis.icon(name),
                description=f"{len(help_view.buckets[name])} commands",
            )
            for name in names
        ]
        super().__init__(placeholder=placeholder, options=options)
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.open_category(interaction, self.values[0], 0)


class NavButton(discord.ui.Button):
    def __init__(self, help_view: "HelpView", label: str, style: discord.ButtonStyle, action: str, disabled: bool = False) -> None:
        super().__init__(label=label, style=style, disabled=disabled)
        self.help_view = help_view
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.help_view
        if self.action == "home":
            await view.open_category(interaction, None, 0)
        elif self.action == "previous":
            await view.open_category(interaction, view.category, max(0, view.page - 1))
        elif self.action == "next":
            await view.open_category(interaction, view.category, view.page + 1)
        elif self.action == "automatic":
            await interaction.response.send_message(embed=automatic_embed(view.bot, view.guild), ephemeral=True)


class HelpView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, author_id: int, buckets: dict[str, list[commands.Command]]) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.buckets = buckets
        self.category: Optional[str] = None
        self.page = 0
        self.render()

    def render(self) -> None:
        self.clear_items()
        if self.category is None:
            children = home_items(self.bot, self.guild, self.buckets)
        else:
            children = category_items(self.category, self.buckets[self.category], self.page)
        children.append(discord.ui.Separator())

        main = [name for name in MAIN_MENU if name in self.buckets]
        others = [name for name in OTHERS_MENU if name in self.buckets]
        if main:
            children.append(discord.ui.ActionRow(CategorySelect(self, "Main Menu", main)))
        if others:
            children.append(discord.ui.ActionRow(CategorySelect(self, "Other Menu", others)))

        pages = page_count(len(self.buckets.get(self.category, []))) if self.category else 1
        on_home = self.category is None
        buttons = [
            NavButton(self, "Automatic Features", discord.ButtonStyle.secondary, "automatic"),
            NavButton(self, "Home", discord.ButtonStyle.primary, "home", disabled=on_home),
            NavButton(self, "Previous", discord.ButtonStyle.secondary, "previous", disabled=on_home or self.page <= 0),
            NavButton(self, "Next", discord.ButtonStyle.secondary, "next", disabled=on_home or self.page >= pages - 1),
        ]
        support = os.getenv("SUPPORT_SERVER_URL", "").strip()
        if support:
            buttons.append(discord.ui.Button(label="Support", style=discord.ButtonStyle.link, url=support))
        children.append(discord.ui.ActionRow(*buttons))
        children.append(discord.ui.TextDisplay(f"-# {ui.BRAND} · Everything is one . away"))
        self.add_item(discord.ui.Container(*children, accent_colour=ACCENT))

    async def open_category(self, interaction: discord.Interaction, name: Optional[str], page: int) -> None:
        self.category, self.page = name, page
        self.render()
        await interaction.response.edit_message(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            embed=ui.warn("Not Your Menu", "Run `.help` yourself to open your own menu."), ephemeral=True
        )
        return False


# -------------------------------------------------------------------------- cog
class HelpCog(commands.Cog, name="Help"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # uploads the category icons once, then only reads them back
        try:
            await prime_emojis.sync(self.bot)
        except Exception as error:
            print(f"Emojis: icons could not be prepared ({type(error).__name__}: {error}). Using the spider emoji instead.")

    async def level_of(self, member: discord.abc.User) -> int:
        if await self.bot.is_owner(member):
            return 2
        if isinstance(member, discord.Member) and member.guild_permissions.administrator:
            return 1
        return 0

    @commands.command(name="help", brief="Show the help menu or details of one command.", extras={"category": "Other", "usage": ".help [command]"})
    @commands.guild_only()
    async def help_prefix(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        level = await self.level_of(ctx.author)
        if command:
            cmd = self.bot.get_command(command.strip().lstrip(".").lower())
            if cmd is None or not visible(cmd, level):
                await ctx.send(embed=ui.warn("Command Not Found", f"There is no command called `{command[:40]}` that you can use."))
                return
            await ctx.send(embed=detail_embed(cmd))
            return
        buckets = collect(self.bot, level)
        view = HelpView(self.bot, ctx.guild, ctx.author.id, buckets)
        await ctx.send(view=view)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if ctx.command is not None and ctx.command.has_error_handler():
            return
        if isinstance(error, (commands.MissingPermissions, commands.NotOwner, commands.MissingRole, commands.MissingAnyRole)):
            text = "This command is restricted to the bot owner." if isinstance(error, commands.NotOwner) else "This command is restricted to server administrators."
            emb = ui.error("Access Denied", text)
        elif isinstance(error, commands.NoPrivateMessage):
            emb = ui.warn("Server Only", "This command can only be used inside a server.")
        elif isinstance(error, commands.MissingRequiredArgument):
            emb = ui.warn("Missing Argument", f"Correct usage: `{usage_of(ctx.command)}`")
        elif isinstance(error, commands.UserInputError):
            emb = ui.warn("Invalid Input", f"I could not understand that. Correct usage: `{usage_of(ctx.command)}`")
        elif isinstance(error, commands.CommandOnCooldown):
            emb = ui.warn("Slow Down", f"Try again in **{error.retry_after:.1f}s**.")
        elif isinstance(error, commands.BotMissingPermissions):
            emb = ui.error("Missing Bot Permissions", "I need: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions))
        elif isinstance(error, commands.CheckFailure):
            emb = ui.error("Not Allowed", str(error) or "You cannot use this command.")
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            log_exception(error, context=f"command {ctx.command} channel=#{ctx.channel}")
            emb = ui.error("Something Went Wrong", "An unexpected error occurred. The issue was logged for the developer.")
        try:
            await ctx.send(embed=emb)
        except (discord.Forbidden, discord.HTTPException):
            pass
