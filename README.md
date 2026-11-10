# Discord Event Bot

This project includes a Discord bot in [main.py](main.py), with shared settings in `botcore/config.py` and SQLite-backed server state in `botcore/storage.py`.

## Features
- Create events and tournaments
- Join/leave buttons
- Start and end event flow
- Bracket generation for tournaments
- Car meet registration form with private staff review and accept/reject buttons
- Join To Create voice channels with a persistent owner-control panel
- Dot-prefix music commands backed by an optional Lavalink v4 node
- Announcements, private voice/audit logs, and destructive-action alerts
- Website-managed private support tickets with category intake, staff assignment, HTML transcripts, and customer ratings

## Setup
1. Install Python 3.12+
2. Open a terminal in this folder
3. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and set the token and car meet IDs:
   ```bash
   copy .env.example .env
   ```
   Then edit `.env`:
   - `DISCORD_TOKEN`: your bot token
   - `DATABASE_PATH`: SQLite database file; existing `data.json` is imported on first startup and left untouched
   - `CARMEET_REVIEW_CHANNEL_ID`: ID of a private text channel visible to the bot and staff
   - `CARMEET_STAFF_ROLE_ID`: role allowed to accept/reject applications; server administrators can also review
   - `CARMEET_ACCEPTED_ROLE_ID`: role granted after acceptance, or leave as `0` to skip role assignment
   - `LOG_CHANNEL_ID`: optional existing private log channel ID; use `0` and run `.setup_logs` to create one
   - `TRUSTED_ROLE_MANAGEMENT_BOT_IDS`: optional comma-separated numeric IDs of role-panel bots whose verified, non-dangerous member-role changes should be allowed; do not add IDs for ordinary bots
   - `LAVALINK_URI`: Lavalink v4 node address, usually `http://127.0.0.1:2333` when hosted on the same VPS; a bare hostname is treated as an HTTPS endpoint
   - `LAVALINK_PASSWORD`: the password configured on that Lavalink node; leave blank to disable music
   - `YOUTUBE_OAUTH_REFRESH_TOKEN`: optional YouTube OAuth token for the Lavalink node; keep it secret and never commit it
   - `MUSIC_SEARCH_SOURCE`: source search prefix, default `scsearch` (SoundCloud); YouTube-based environment settings fall back to SoundCloud on startup

Ticket support requires Manage Channels permission. Configure the panel and a
private transcript channel from the website dashboard.

To configure Dino, enable Developer Mode in Discord under **User Settings > Advanced**,
right-click Dino in the member list, and choose **Copy User ID**. Put that numeric ID
in `.env` as `TRUSTED_ROLE_MANAGEMENT_BOT_IDS=<DINO_NUMERIC_USER_ID>` (replace the
placeholder; do not leave the angle brackets). This only allows fresh, verified,
non-dangerous member-role changes by that bot. Dangerous permission grants and the
other anti-nuke checks remain active.

5. Run the bot:
   ```bash
   python main.py
   ```

## Railway persistent storage

Subscriptions and other bot state are stored in SQLite. On Railway, attach a
persistent volume to the service at `/data`. The bot automatically places a
relative `DATABASE_PATH` (including the default `data.sqlite3`) inside
Railway's volume mount. On first startup with the volume, the bot copies an
existing SQLite database from the deployment folder into the volume if one is
present. Redeploy after attaching the volume. Do not set `DATABASE_PATH` to a
location outside `/data` or keep the database only in the service's deployment
files, because replacing those files can remove the database and its
subscription records.

## Railway music setup

Music needs a separate Lavalink v4 service; the bot cannot play audio by itself.
This repository includes a Railway-ready Lavalink image configuration in
`lavalink/`, including the YouTube source plugin and multiple playback clients.
If a YouTube search result is unavailable, the bot tries the next matching
search results automatically.
The Lavalink config uses a 4-second playback buffer and Opus encoding
quality 8 for a balance between smooth playback and clear audio with low
latency. The bot also raises the voice channel bitrate to 96 kbps when it
joins, if it has the Manage Channels permission.
Music searches compare results from the configured source and all available
fallback sources before selecting the best plain-text match. On Railway,
YouTube-backed searches are skipped until YouTube OAuth is authorized because
the node's datacenter IP may be blocked.
For plain-text searches, the bot skips results marked as alternate versions or
short edits (such as "slowed", "remix", "intro", or "edit") when those terms
were not part of the query. Exact and title-leading matches rank ahead of
results that only contain the query as one extra word (for example, a song
named "Khat" ranks ahead of "Rooye Khat" for the query "khat"). Weak matches
are discarded instead of being played as an unrelated song. Search results
also prefer titles in the same writing system as the query and prefer official,
VEVO, or Topic uploads when the source identifies them. These signals improve
ranking but cannot guarantee an official result if the music source does not
return one. If a SoundCloud preview ends early, recovery searches with the
artist and title plus "full song" before giving up on a full-length copy.
Spotify track, playlist, and album links are resolved by the Lavalink LavaSrc
plugin and played through YouTube matches; the bot does not stream audio from
Spotify itself. It first matches by Spotify ISRC, then uses YouTube Music search
by title and artist when no ISRC match is available. The Lavalink container
also runs Spotify Tokener locally to provide the anonymous access token required
by Spotify playlist links. This keeps it in the existing Railway service and
prefers LavaSrc's Spotify Partner API for link resolution. Its browser may use
additional memory and CPU when resolving Spotify links. Because Railway
restricts Chrome's normal sandbox, the helper runs Chrome without that sandbox
under a separate unprivileged user and without Lavalink environment secrets.
Complete the YouTube OAuth setup in [`lavalink/README-OAUTH.md`](./lavalink/README-OAUTH.md)
for reliable Spotify-link playback on Railway. SoundCloud may only offer a
short preview for some tracks; if a stream ends early and no full-length
alternative is available, the bot skips it instead of replaying the same
fragment.

1. In the Railway project, add a service from this same GitHub repository and
   name the service `lavalink`.
2. In that service's Settings, set the Root Directory to `/lavalink`. Railway
   will build its Dockerfile and deploy Lavalink on port `2333`. The same
   container runs the private Spotify token helper on localhost; no extra
   Railway service or public port is needed. Do not configure an HTTP
   healthcheck path: Lavalink protects `/v4/info` with its node password, so
   Railway's unauthenticated healthcheck receives HTTP 401.
3. Add a generated secret variable named `LAVALINK_SERVER_PASSWORD` to the
   `lavalink` service. Do not publish or commit this secret.
4. In the bot service's Variables, set:
   - `LAVALINK_URI` to `http://${{lavalink.RAILWAY_PRIVATE_DOMAIN}}:2333`
   - `LAVALINK_PASSWORD` to `${{lavalink.LAVALINK_SERVER_PASSWORD}}`
   - `MUSIC_SEARCH_SOURCE` to `scsearch` until YouTube OAuth is authorized
5. Apply the variables and wait for both services to finish deploying. Keep the
   Lavalink service private; it does not need a public domain.

The `lavalink` service reference above must match its Railway service name.
Railway resolves these reference variables automatically and restarts the bot
when its variables change.
To enable YouTube on datacenter-hosted nodes, complete the burner-account
authorization and add `YOUTUBE_OAUTH_REFRESH_TOKEN` as a secret variable on the
Lavalink service; see [the YouTube OAuth steps](./lavalink/README-OAUTH.md).
After Lavalink redeploys with the token, change the bot's search source to
`ytmsearch` in Discord with `.music source ytmsearch` if desired.

## Automatic GitHub sync

VS Code starts the `GitHub auto-sync` task when this folder is opened. After a
code file is saved, the task creates a commit and pushes it to the current
branch on `origin`. GitHub authentication must already be configured (for
example with Git Credential Manager or SSH). If a push fails, the commit stays
local and the task reports the command to retry.

The sync includes Python, PowerShell, JSON, Markdown, text, YAML, and related
code/configuration files. `.env`, databases, virtual environments, and Python
cache files remain excluded.

## Important
The script loads `DISCORD_TOKEN` and other settings from `.env`.
On Windows PowerShell, you can set it temporarily like this:

```powershell
$env:DISCORD_TOKEN="your_token_here"
python main.py
```

## Bot commands
- `.announce <message>`: post an announcement (requires Manage Server)
- `/announce`: post a formatted announcement in this or a selected text channel; optional title (requires Manage Server)
- `.modhistory @member [1-10]`: show recent moderation actions (requires Administrator and View Audit Log)
- `.purgeuser @member [1-100]`, `.purgebots [1-100]`: remove recent messages from the selected member or bots (requires Administrator)

- `.announce <message>`: post an announcement (requires Manage Server; `@everyone`/`@here` can ping, role pings are disabled)
- `.invites [member]`: show tracked invite counts; `.invites leaderboard` shows the server ranking

- `.syncslash`: sync the latest global slash commands directly to this server (requires Administrator)
- `.jtc setup`: create the Join To Create voice hub and its control panel (requires Manage Channels and Move Members)
- `.jtc panel [#channel]`: post the persistent K.R.Y.O voice control panel (requires Administrator)
- `.jtc name`, `.jtc limit`, `.jtc lock`, `.jtc unlock`, `.jtc claim`: manage your temporary voice channel
- `.carmeet_setup #review-channel @staff-role [@participant-role]`: configure car-meet review for this server
- `.setup_logs`: create a private admin log channel (requires Administrator and active Plus/Premium)
- `.premium tiers`: show monthly plans
- `.premium status`: show this server's plan (requires Manage Server)
- `.premium grant <guild_id> <plus|premium> [days]` and `.premium revoke <guild_id>`: bot-owner-only manual activation after verifying UPI outside Discord
- `.play <song name or URL>` (alias `.p`)
- `.pause`, `.resume`, `.skip`, `.stop`, `.disconnect`
- `.queue` (alias `.q`), `.nowplaying` (alias `.np`), `.volume <1-200>`
- `/music play`, `/music pause`, `/music resume`, `/music skip`, `/music stop`
- `/music queue`, `/music nowplaying`, `/music volume`, `/music shuffle`, `/music loop`, `/music seek`, `/music remove`, `/music clear`
- Playing a song posts the English-language music panel with previous, pause/resume, skip, stop, repeat, volume, seek, lyrics, radio, and disconnect controls. The panel updates for each track and retries stalled or failed playback.
- `/carmeet_panel` (Manage Events permission): post the public registration button in the current channel
- `/antinuke on`, `/antinuke off`, `/antinuke status`, `/antinuke limits` (Administrator; alert limits require Plus/Premium)
- `/antinuke whitelist add <member>`, `/antinuke whitelist remove <member>`, `/antinuke whitelist list` (Administrator)
- `/automod on`, `/automod off`, `/automod status`, `/automod limits`, `/automod invite_filter` (Administrator; filters require Plus/Premium)
- `/moderation ban`, `/moderation unban`, `/moderation kick`, `/moderation timeout`, `/moderation untimeout`
- `/moderation warn`, `/moderation warnings`, `/moderation clear_warnings`, `/moderation purge`, `/moderation slowmode`
- `/vc lock <voice_channel>` (Administrator, Plus/Premium): deny @everyone's Connect permission while saving its previous value
- `/vc unlock <voice_channel>` (Administrator): restore the saved Connect permission
- `/event_create <name> [description]`
- `/event_start <event_id> [image_url]` (optional HTTP/HTTPS image link shown in the start announcement)
- `/event_end <event_id>`
- `/tournament_create <name> [max_players] [description]`
- `/tournament_start <tournament_id>`
- `/tournament_report <tournament_id> <match_no> <winner>`
- `/tournament_bracket <tournament_id>`

The Join To Create panel uses Discord Components V2 and the K.R.Y.O spider banner in
[`assets/jtc/voice_interface_banner.png`](./assets/jtc/voice_interface_banner.png).
Buttons are persistent across bot restarts and act on the temporary voice channel
the owner is currently in. `Permit`/`Invite` grant one member access; `Permit All`
opens the channel to everyone. Ownership transfer requires the selected member to
be in the channel, while `Claim` is available only after the recorded owner has
left. The bot needs **Manage Channels** and **Move Members** for setup, plus the
permissions needed to edit channel overwrites and attach the panel banner.

Members using the admin slash commands need the `Manage Events` permission. Give the bot access to the private review channel and permission to send messages and embeds there. To assign the accepted role, give the bot `Manage Roles` and move its role above the participant role in Server Settings.

## Premium Plans
Subscriptions are monthly and manually approved: after verifying a UPI payment outside Discord, the bot owner runs `.premium grant <guild_id> plus 30` or `.premium grant <guild_id> premium 30`. The owner can revoke access with `.premium revoke <guild_id>`. Expired plans automatically lose gated features. This is not automatic payment processing and the bot does not store payment credentials.

On grant, the bot's nickname in that server becomes `KYRO PLUS` or `KYRO PREMIUM` by default. The activation embed says **PLUS ACTIVATED** or **PREMIUM ACTIVATED**, matching the purchased tier. `PREMIUM_TICK_EMOJI` defaults to `✅`; it can be set to an animated custom emoji mention available to the bot. On revoke or expiry, it restores the nickname saved at the first grant. The bot needs **Change Nickname** permission; the bot's global username is never changed. Change `PREMIUM_NICKNAME_BASE` in `.env` to customize the base name; the 32-character Discord nickname limit applies.

Plus (₹200/month) gates private voice/security logs and destructive-action alerts. Premium (₹500/month) includes Plus and best-effort vanity-change alerts. Premium does not guarantee vanity recovery. Discord does not expose device hardware IDs, so hardware-level anti-alt tracking is not available. The bot cannot prevent the server owner or an administrator with overriding permissions from changing the server. Avoid advertising those guarantees.

## Security snapshots and emergency lockdown
With an active PRIME plan, administrators can use `.backup [label]` or `/backup`
to save server configuration, `.restore [snapshot]` or `/restore` to restore
the latest snapshot, and `.lockdown on|off [minutes]` or `/lockdown` to stop
messages and thread activity in non-staff channels. `.panic` or `/panic`
restores the latest snapshot first, then applies the lockdown so restored
channel permissions cannot accidentally reopen the server. Give the bot
**Administrator** for a full snapshot restore; lockdown also attempts
channel-level permission changes when the bot lacks Administrator. Channels
named for staff, moderation, security, audit, or logs are left available.
Snapshots cover server configuration, not message history or the original
emoji/sticker image files.

## Music Setup
Music needs a running Lavalink v4 service on the VPS; Wavelink connects the bot to that audio node. Start/configure the node before expecting playback, set its address and password in `.env`, and do not expose the Lavalink port publicly. The selected `MUSIC_SEARCH_SOURCE` must be supported by a source manager/plugin enabled on the node. Search and availability depend on that source; the bot cannot guarantee every song or a fixed premium bitrate. If Lavalink is offline, the bot still starts and music commands report that music is unavailable.

Utility commands are also available as both prefix and slash commands:
`servericon`, `serverbanner`, `roleinfo`, `channelinfo`, `permissions`,
`roles`, `channels`, `inrole`, `emojis`, `stickers`, `choose`, `coinflip`,
`roll`, `eightball`, and `timestamp`. Dice rolls accept `d20` or `3d6`;
timestamps accept ISO 8601 dates and use UTC when no timezone is supplied.
More member and server utilities include `joinedat`, `createdat`, `toprole`,
`rolecount`, `channelcount`, `humancount`, `botcount`, `boosters`, `colorinfo`,
and `snowflake`. Text tools include `textcase`, `charcount`, `wordcount`,
`jsonformat`, `base64 encode|decode`, `urlencode`, `urldecode`, and `unicode`.

Slash moderation controls are Administrator-only until a more specific permission policy is configured. Give the bot **Manage Channels** for log creation/VC locks and **View Audit Log** to identify destructive-action actors. The current anti-nuke feature reports bursts; it does not auto-ban or undo destructive actions. VC unlock restores only the Connect permission value this bot saved when it locked the channel. Enable **Message Content Intent** in the Discord Developer Portal for dot-prefix commands. Voice durations are tracked in memory and reset when the bot restarts. With a ₹0 hosting budget, the bot only stays online while your own PC and process are running. Install dependencies with `python -m pip install -r requirements.txt`.


Set `MUSIC_ENABLED=false` to unload the music extension and its background connection/recovery tasks when music is not in use; it defaults to `true`. Railway's Lavalink start command is set to `true` while music is disabled; restore `sh /opt/Lavalink/start-services.sh` and set `MUSIC_ENABLED=true` to re-enable playback.

Slash moderation controls are Administrator-only until a more specific permission policy is configured. Give the bot **Manage Channels** for log creation/VC locks and **View Audit Log** to identify destructive-action actors. The current anti-nuke feature reports bursts; it does not auto-ban or undo destructive actions. VC unlock restores only the Connect permission value this bot saved when it locked the channel. Enable **Message Content Intent** in the Discord Developer Portal for dot-prefix commands. Voice durations are tracked in memory and reset when the bot restarts. With a ₹0 hosting budget, the bot only stays online while your own PC and process are running. Install dependencies with `python -m pip install -r requirements.txt`.

The PRIME-gated `.antipromo on` / `/antipromo on` protection also scans up to
four attached images per message for casino offers or game promotions combining
bonus activation with payout terms using local OCR, then deletes matching
messages without automatically punishing the sender. Images attached to
forwarded messages are scanned as well. This image filter also applies to
server admins and the owner unless they whitelist themselves for `antipromo`.
Give the bot **Manage Messages**. OCR is limited to images up to 8 MiB and is
best-effort: image download, OCR, and Discord deletion depend on network and
server load, so a 0.1-second deletion cannot be guaranteed.

With `antimention` or `antipromo` enabled, an unwhitelisted bot/app message
that mass-mentions `@everyone`/`@here` is deleted and the sender bot is banned
from that server; the rule works even if Discord does not provide message text.
If an invite is present, it is also recorded in the incident log. Whitelist
trusted bots with `/whitelist add`. This reacts only after Discord delivers the
message; to prevent the first mass ping from being visible, remove **Mention
@everyone, @here, and All Roles** from untrusted bot roles in Server Settings →
Roles. A bot with Administrator can bypass role overwrites. Give KRYO **Manage
Messages** and **Ban Members**, with its role above the app/bot roles, for full
response.


## Web dashboard setup

The website's dashboard API runs alongside the bot. Configure these environment
variables in Railway; keep the Discord client secret private:

- `DASHBOARD_DISCORD_CLIENT_ID`: the Discord application's client ID
- `DASHBOARD_DISCORD_CLIENT_SECRET`: its OAuth2 client secret
- `DASHBOARD_FRONTEND_URL`: the website origin, such as `https://kryo-web.vercel.app`
- `PORT`: supplied by Railway

Add `https://kryo-web.vercel.app/api/auth/callback` as an OAuth2 redirect URI in
the Discord Developer Portal. The OAuth application must permit the `identify`
and `guilds` scopes. In Vercel, set `KRYO_API_BASE_URL` to the bot's public
Railway HTTPS origin (for example, `https://your-bot.up.railway.app`), then
redeploy both services.

Dashboard access requires Discord sign-in and owner, Administrator, or Manage
Server permission in a server where KRYO is online. The dashboard edits the
bot's stored security modules, welcome and leave messages, autoroles, log
channels, and antinuke whitelist. Enabling protections and per-event log routing
are Prime-gated, consistent with the bot's feature checks. The dashboard does
not expose unsupported welcome image/DM options or general-purpose automod
rules. Discord dashboard sessions are kept in the bot's SQLite state for up to
30 days and refreshed automatically, so a bot restart or reopening the website
does not require another authorization. Signing out removes the saved session.

channels, welcome embed image URLs, antinuke whitelist, and invite tracking
settings. The Announcement page sends embeds directly to a chosen text channel;
server-wide pings are off by default and require an explicit confirmation and
the bot's Mention Everyone permission. Invite tracking requires the bot's
Manage Server permission and the Server Members and Guild Invites intents.
It records join attribution, flags accounts younger than the configurable age,
marks invitees who leave, and ranks inviters by active non-flagged invitees.
Attribution may be unavailable when invite uses change simultaneously or the
invite link is deleted. Welcome images must be publicly accessible HTTP(S)
URLs so Discord can load them. The Jail page creates a permission-free KRYO
Jail role and lets dashboard managers jail/release members by exact username
or Discord ID. Moderators with Manage Roles can also use `/jail` and `/unjail`
or the `.jail` and `.unjail` prefix commands.
Jailed members retain their existing channel visibility but cannot send
messages, react, create threads, use slash commands, or join/speak in voice.
Original channel-specific member permissions are restored on release. KRYO
needs Manage Roles, and Move Members if the target is already connected to
voice. Role selectors show every role below KRYO; autorole marks roles that
cannot safely be assigned. Enabling protections
and per-event log routing are Prime-gated, consistent with the bot's feature
checks. The dashboard does not support welcome DMs or general-purpose automod
rules. Discord dashboard sessions are kept in the bot's SQLite state for up to
30 days and refreshed automatically, so a bot restart or reopening the website
does not require another authorization. Signing out removes the saved session.
Whitelist entries show Discord usernames, and each protection set to Timeout
can use a configurable duration from 5 minutes up to Discord's 28-day limit.
Anti-Spam keeps its existing 10-minute timeout by default.

