from botcore.moderation_embeds import (
    build_ban_embed,
    build_caps_embed,
    build_link_blocked_embed,
    build_moderation_action_dm,
    build_notice_dm,
    build_promo_removed_embed,
    build_unban_embed,
    build_warning_dm,
    moderation_emojis_for,
)


def test_ban_embed_has_reason_and_red_accents() -> None:
    embed = build_ban_embed(username="Example User", reason=None, emojis={})

    assert embed.color.value == 0xF23F43
    assert embed.title == "🔨 Member banned"
    assert "**Example User** was banned." in embed.description
    assert "**Reason** — No reason given" in embed.description


def test_unban_embed_has_green_access_restored_copy() -> None:
    embed = build_unban_embed(username="Example User", emojis={})

    assert embed.color.value == 0x23A55A
    assert embed.title == "🔓 Member unbanned"
    assert "**Status** — Can rejoin the server" in embed.description


def test_promo_embed_uses_plain_user_mention_and_violet_color() -> None:
    embed = build_promo_removed_embed(user_id=123, emojis={})

    assert embed.color.value == 0x7C3AED
    assert "<@123>, gambling/casino promotional images are not allowed here." in embed.description
    assert "**Action** — Image removed" in embed.description


def test_caps_embed_reports_deleted_message() -> None:
    embed = build_caps_embed(user_id=123, emojis={})

    assert embed.color.value == 0xC026D3
    assert "<@123>, please do not write in all caps." in embed.description
    assert "**Action** — Message removed" in embed.description
    assert "**Tip**" not in embed.description


def test_link_embed_uses_bright_purple_copy() -> None:
    embed = build_link_blocked_embed(user_id=123, emojis={})

    assert embed.color.value == 0xA855F7
    assert "<@123>, posting links is disabled in this server." in embed.description
    assert "**Action** — Message removed" in embed.description


def test_custom_emoji_urls_are_used_as_author_icons() -> None:
    embed = build_ban_embed(
        username="Example User",
        reason="Test",
        emojis={"GAVEL_URL": "https://cdn.example/gavel.gif"},
    )

    assert embed.title is None
    assert embed.author.name == "Member banned"
    assert embed.author.icon_url == "https://cdn.example/gavel.gif"


def test_moderation_action_dm_uses_animated_gavel_and_safe_reason() -> None:
    embed = build_moderation_action_dm(
        guild_name="KRYO",
        action="banned",
        reason="@everyone **spam**",
        moderator="Staff",
        emojis={"GAVEL_URL": "https://cdn.example/gavel.gif", "ARROW_RED": "<a:arrow:1>"},
    )

    assert embed.author.name == "You were banned"
    assert embed.author.icon_url == "https://cdn.example/gavel.gif"
    assert "<a:arrow:1> **Server** — KRYO" in embed.description
    assert "@\u200beveryone" in embed.description
    assert "\\*\\*spam\\*\\*" in embed.description


def test_warning_dm_uses_cached_animation_and_reports_count() -> None:
    embed = build_warning_dm(
        guild_name="KRYO",
        reason="Be respectful",
        total_warnings=3,
        emojis={"SHIELD_URL": "https://cdn.example/shield.gif", "CHEVRONS_PURPLE": "<a:chevrons:2>"},
    )

    assert embed.author.icon_url == "https://cdn.example/shield.gif"
    assert "**Total warnings** — 3" in embed.description
    assert "> <a:chevrons:2>" in embed.description


def test_notice_dm_falls_back_to_static_emoji() -> None:
    embed = build_notice_dm(
        title="Voice channel invite",
        message="You were invited.",
        emojis={},
    )

    assert embed.title == "🛡️ Voice channel invite"
    assert "➤ **Notice** — You were invited." in embed.description


def test_moderation_emojis_merge_guild_cache_with_fallbacks() -> None:
    class FakeBot:
        def get_cog(self, name):
            assert name == "MassMentionCog"
            return type("FakeCog", (), {"emojis": {123: {"GAVEL": "<a:kryo_gavel:4>"}}})()

    emojis = moderation_emojis_for(FakeBot(), 123)

    assert emojis["GAVEL"] == "<a:kryo_gavel:4>"
    assert emojis["ARROW_RED"] == "➤"
