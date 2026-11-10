# YouTube OAuth fix — step by step

YouTube can block datacenter IPs (Railway, DigitalOcean, Fly, ...) with
"Sign in to confirm you're not a bot". When that happens the bot reports:

> Playback stopped: YouTube is refusing the node's requests.

The `application.yml` in this folder enables OAuth for the YouTube `TV` client.
OAuth does not authenticate every client, and neither OAuth nor a PO token is
guaranteed to make every video playable. Never commit tokens or paste them into
chat.

---

## Quick workaround (works right now, no setup)

In Discord, run:

```
.music source scsearch
```

Music then comes from SoundCloud and YouTube is skipped entirely. Use this until
the YouTube authentication setup below is done. SoundCloud may only have a short
preview for some songs; the bot skips an early-ending copy if it cannot find a
full-length alternative.

Spotify links provide track metadata, not playable audio. On Railway, LavaSrc
matches Spotify tracks to YouTube audio, so complete the OAuth setup below for
reliable Spotify-link playback.

---

## Permanent fix (OAuth)

### 1. Deploy the OAuth-enabled node

Deploy the latest `lavalink/` configuration. On Railway, set
`MUSIC_SEARCH_SOURCE=scsearch` on the bot until authorization is complete.
For local use, start Lavalink from this folder so it loads `application.yml`:

```powershell
cd d:\disocrd\lavalink
$env:LAVALINK_SERVER_PASSWORD = (Get-Content ..\.env | Where-Object { $_ -match '^LAVALINK_PASSWORD=' } | Select-Object -First 1).Substring('LAVALINK_PASSWORD='.Length)
$oauthLine = Get-Content ..\.env | Where-Object { $_ -match '^YOUTUBE_OAUTH_REFRESH_TOKEN=' } | Select-Object -First 1
if ($oauthLine) { $env:YOUTUBE_OAUTH_REFRESH_TOKEN = $oauthLine.Substring('YOUTUBE_OAUTH_REFRESH_TOKEN='.Length) }
java -jar ..\Lavalink.jar
```

### 2. Complete the sign-in

1. Open the Lavalink service → **Deploy Logs**.
2. Find a line from `dev.lavalink.youtube` with a device-code URL, e.g.
   `https://www.google.com/device` and a code like `ABCD-EFGH`.
3. Open that URL in a browser, enter the code.
4. Sign in with a **burner Google account** — never your main account. A
   throwaway account is all this needs.
5. Watch the log again. It prints:

   ```
   Refresh token: 1//0xxxxxxxxxxxxxxxxxxxx
   ```

6. Copy the refresh token directly into the Lavalink service's secret
   environment variable in the next step. Do not put it in source control or
   send it in chat.

If no device-code URL appears, check the Lavalink service's Deploy Logs and
confirm the YouTube plugin loaded.

### 3. Save the token as a secret

On Railway, add `YOUTUBE_OAUTH_REFRESH_TOKEN` as a secret variable on the
Lavalink service and redeploy it. Locally, put the value in `.env` (which must
remain untracked); the local startup command above exports it before launching
Lavalink:

Restart or redeploy Lavalink after adding the variable.

### 4. PO-token configuration

The YouTube plugin accepts a PO token and matching visitor data for supported
clients, and `application.yml` has optional `YOUTUBE_PO_TOKEN` and
`YOUTUBE_VISITOR_DATA` settings. However, the previously documented
`youtube-trusted-session-generator` project is deprecated and no longer works
with YouTube's current token flow. Do not use it to generate values. Leave
these variables unset unless you have a current, trusted generation method;
OAuth is the supported setup described above.

### 5. Test

In Discord:

```
.music source ytmsearch
.play kesariya
```

If the song plays, YouTube is fixed. You can then change the bot's
`MUSIC_SEARCH_SOURCE` to `ytmsearch` or run `.music source ytmsearch`.

---

## Notes

- **A burner account is required.** YouTube may flag the account; do not use one
  you care about.
- **The token can expire** if the account is flagged or the password changes.
  If YouTube stops working later, repeat steps 2 and 3 to get a fresh token.
- **The `TV` client** is the one that supports OAuth sign-in. It is already in
  the client list in `application.yml`.