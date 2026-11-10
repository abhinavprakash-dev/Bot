# Whitelist embeds

Whitelist add/remove behavior and storage remain in `SecurityCog`. The success
and missing-entry responses now use compact purple embeds with the animated
`kryo_lock` and `kryo_check` emojis. Static fallbacks (`🔒` and `✅`) are used
when the bot cannot upload or use the custom emojis.

Administrators can preview both embeds with `/testwhitelist`.

The bot needs **Send Messages**, **Embed Links**, **Use External Emojis**, and
**Manage Guild Expressions**. Manage Guild Expressions is only needed to upload
the optional animated emojis.
