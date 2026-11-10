# Mass mention warning

The bot blocks raw or resolved `@everyone` and `@here` mentions from members
without Mention Everyone permission. Administrators and roles listed in
`MASS_MENTION_WHITELIST_ROLE_IDS` are exempt. Warnings are stored in the
existing SQLite state and decay after `MASS_MENTION_DECAY_HOURS` (24 by
default).

The ladder is defined in `cogs/mass_mention.py`:

1. Warn
2. Timeout for 10 minutes
3. Kick
4. Ban

Set `LIVE_ANIMATION=true` only if the extra message edits are wanted. The
optional animated emoji assets are loaded from `assets/kryo-embed/`; missing
assets, missing permissions, and full emoji slots use the static fallbacks.

## Required permissions

The bot needs **View Channel**, **Read Message History**, **Send Messages**,
**Embed Links**, **Manage Messages**, **Moderate Members**, **Kick Members**,
**Ban Members**, and **Manage Guild Expressions**. Role hierarchy must also
place the bot above members it must punish.

Use `/testwarning level:1` through `/testwarning level:4` as an administrator
to preview the embed without changing warning state or punishing anyone.

## Local run

```powershell
copy .env.example .env
# set DISCORD_TOKEN and any optional settings in .env
.\.venv\Scripts\python.exe main.py
```

## Railway deployment

```powershell
railway login
railway link
railway variables set DISCORD_TOKEN=your_token
railway up
```

Attach a persistent Railway volume and set `RAILWAY_VOLUME_MOUNT_PATH=/data`
so warning counts survive redeploys. Do not commit `.env` or the token.
