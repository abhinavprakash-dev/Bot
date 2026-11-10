# Link filter

The link filter removes HTTP(S) URLs, `www.` links, Discord invites, markdown
links, configured bare domains, message embeds, and link buttons. Administrators,
members with Manage Messages, configured roles, and configured channels are
exempt. Whitelist values are set in `.env`:

```text
LINK_FILTER_WHITELIST_ROLE_IDS=
LINK_FILTER_WHITELIST_CHANNEL_IDS=
LINK_FILTER_WHITELIST_DOMAINS=youtube.com,youtu.be,spotify.com
LINK_FILTER_DELETE_DELAY=8
LINK_FILTER_LOG_CHANNEL_ID=0
```

Set `LINK_FILTER_DELETE_DELAY=0` to keep warning messages. `/testlink` previews
the compact embed without deleting a message.

Required permissions: **Manage Messages**, **Send Messages**, **Embed Links**,
**Use External Emojis**, and **Manage Guild Expressions**. The last permission
is only needed to upload the animated `kryo_linkoff` emoji; the static fallback
is used otherwise.
