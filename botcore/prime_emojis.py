"""Purple category icons for the help menu.

Put this file in the botcore/ folder.
Upload the purple PNGs as application emojis, then paste each emoji ID below.
Until an ID is filled in, a purple circle is shown instead, so nothing breaks.
"""

FALLBACK = "\U0001F7E3"  # purple circle

# category label (as used in help.py) -> (emoji name, emoji ID)
_ICONS = {
    "Anti Nuke": ("antinuke", None),
    "Auto Mod": ("automod", None),
    "Role Config": ("roleconfig", None),
    "Moderation": ("moderation", None),
    "Smart Moderation": ("smartmod", None),
    "Join To Create": ("j2c", None),
    "No Prefix": ("noprefix", None),
    "Other": ("other", None),
    "Auto Role": ("autorole", None),
    "Welcomer": ("welcomer", None),
    "Self Roles": ("selfroles", None),
    "Utility": ("utility", None),
    "Voice": ("voice", None),
    "Music": ("music", None),
}


def icon(category: str) -> str:
    """Return the emoji string for a help category."""
    name, emoji_id = _ICONS.get(category, ("x", None))
    if not emoji_id:
        return FALLBACK
    return f"<:{name}:{emoji_id}>"
