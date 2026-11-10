# Moderation embeds

The moderation result embeds are built in `botcore/moderation_embeds.py`.
Existing ban, unban, promotional-image, caps, and link detection/action logic
is unchanged. The bot uses animated custom emojis when available and static
fallbacks otherwise. Existing mass-mention emoji loading uploads and caches
the additional manifest entries on startup; guilds without enough emoji slots
use fallbacks for the remainder.

Administrators can preview all five embeds with `/testembeds`.

The bot needs **Send Messages**, **Embed Links**, and **Use External Emojis**.
**Manage Guild Expressions** is needed to upload the animated emoji assets.
Existing moderation permissions are still required for bans and message
deletion.

Promo-image warning visibility is controlled by `PROMO_IMAGE_WARNING_DELETE_DELAY`
(default `8` seconds; `0` keeps it). Link warnings continue to use
`LINK_FILTER_DELETE_DELAY` (default `8` seconds; `0` keeps them).
