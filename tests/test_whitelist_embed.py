from types import SimpleNamespace

from botcore.whitelist_embed import (
    build_already_whitelisted_embed,
    build_not_whitelisted_embed,
    build_whitelist_updated_embed,
)


def test_not_whitelisted_embed_uses_purple_access_denied_copy() -> None:
    user = SimpleNamespace(mention="<@123>")

    embed = build_not_whitelisted_embed(
        user=user,
        category="All protections",
        emojis={"LOCK": "🔒"},
    )

    assert embed.color.value == 0x8B5CF6
    assert embed.title == "🔒 Not whitelisted"
    assert "Access denied" in embed.description
    assert "<@123> is not whitelisted in **All protections**." in embed.description


def test_whitelist_updated_embed_supports_removed_action() -> None:
    user = SimpleNamespace(mention="<@123>")

    embed = build_whitelist_updated_embed(
        user=user,
        category="Anti-link",
        action="removed",
        emojis={"CHECK": "✅"},
    )

    assert embed.color.value == 0xA855F7
    assert embed.title == "✅ Whitelist updated"
    assert "<@123> was removed from the whitelist for **Anti-link**." in embed.description
    assert "still punished if they try to nuke the server" in embed.description


def test_already_whitelisted_embed_uses_compact_purple_style() -> None:
    user = SimpleNamespace(mention="<@123>")

    embed = build_already_whitelisted_embed(
        user=user,
        category="All protections",
        emojis={"CHECK": "✅"},
    )

    assert embed.color.value == 0xA855F7
    assert embed.title == "✅ Already whitelisted"
    assert "<@123> is already whitelisted in **All protections**." in embed.description
