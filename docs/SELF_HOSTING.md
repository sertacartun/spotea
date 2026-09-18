# Self-hosting Spotea

The [README](../README.md) gets you running with two commands. This page
covers the rest:

- [Running with Docker](#running-with-docker)
- [Updating](#updating)
- [HTTPS: using it on your phone](#https-using-it-on-your-phone)
  - [With Tailscale (recommended)](#with-tailscale-recommended)
  - [On the public internet](#on-the-public-internet)
- [Running without Docker](#running-without-docker)
- [Configuration](#configuration)

## Running with Docker

```bash
git clone https://github.com/sertacartun/spotea.git && cd spotea
docker compose up -d
```

Open `http://localhost:8000` and register an account.

No `.env` is needed. Create one (`cp .env.example .env`) only when you want
to change a setting from the [table below](#configuration).

Everything the app keeps lives in `./data` on the host, and it survives
restarts and rebuilds:

| Path | What it is |
|---|---|
| `data/spotea.db` | The SQLite database: accounts, artists, playlists |
| `data/storage/` | Downloaded audio, one folder per account |
| `data/avatars/`, `data/thumbnails/` | Cached artwork |
| `data/secret_key` | The key that signs login sessions, generated on first start. Delete it and everyone is logged out |

To back up the database while the app is running, run
`./scripts/backup.sh`. It needs the `sqlite3` CLI on the host.

## Updating

```bash
git pull
docker compose up -d --build
```

Rebuilds don't touch `./data`.

Spotea tells you when there is something to pull: once every 12 hours the
server asks GitHub for the newest release and, if it is newer than the one
running, says so in **Settings → About**, with a link to the release notes.
Only the first account registered on the instance sees it — on a server
shared with a household, nobody else can run the command anyway.

That check is one request per install, made by the server. The app never
contacts GitHub from anyone's browser, and sends nothing about you or your
library. The owner can turn it off from **Settings → About** — no .env edit
or restart — or shut off the whole feature at deploy time with
`UPDATE_CHECK=false`.

<details>
<summary>Upgrading from an older version</summary>

**Save-for-later removal.** The first start after this update drops the
`content.is_saved` column automatically. The app couldn't add any track
while that column was still there. Nothing you had saved is carried over,
because the feature is gone, not moved. Run `./scripts/backup.sh` first if
you want a copy of the old data.

**Music-only rewrite.** Upgrading across this version needs a fresh
database. Feeds became artists and profiles were folded into the account,
and no migration was written for that change. Move `./data/spotea.db` aside,
start the app, and register again.

</details>

## HTTPS: using it on your phone

Spotea works over plain `http://`, but a browser only turns on some features
for a secure (HTTPS) origin. `http://localhost` is the one exception.

| Feature | Plain `http://` | HTTPS |
|---|---|---|
| Browsing, playing, downloading to the server | ✅ | ✅ |
| Keeping songs on the device | ✅ | ✅ |
| Opening the app with no connection | ❌ | ✅ |
| Installing to the home screen as an app | ⚠️ looks installed, but can't open offline | ✅ |

Offline launch needs a service worker, and browsers only register one on a
secure origin. iOS lets you "Add to Home Screen" either way, so a plain-HTTP
install looks right until the first time you open it without a connection.

### With Tailscale (recommended)

[Tailscale](https://tailscale.com) puts your server and your phone on a
private network and gives the server a real HTTPS certificate. You don't
open any ports, and only your own devices can reach the app.

**1. Set up the tailnet.** Install Tailscale on the server and the phone
and log in to the same account. In the
[admin console](https://login.tailscale.com/admin/dns), turn on both
**MagicDNS** and **HTTPS Certificates**.

**2. Serve Spotea over HTTPS.** On the server:

```bash
sudo tailscale serve --bg 8000
```

`--bg` keeps it running after you close the terminal and across reboots.
Use your `HOST_PORT` if you changed it. On Linux, changing the serve config
needs root.

**3. Find your URL:**

```bash
tailscale serve status
```

It looks like `https://<machine-name>.<tailnet-name>.ts.net`. Open it on the
phone. The first request can take a few seconds while the certificate is
issued.

**4. Lock it to HTTPS.** Create `.env` with:

```bash
# Only reachable from this machine, so the HTTPS URL is the only way in
HOST_PORT=127.0.0.1:8000
# Send the login cookie over HTTPS only
SESSION_HTTPS_ONLY=true
```

Then apply it with `docker compose up -d`. `tailscale serve` connects to
`127.0.0.1`, so it keeps working.

**5. Install the app.** Open the HTTPS URL on the phone, log in once, then
add it to the home screen: Share → Add to Home Screen on iOS, or the menu →
Install app on Android.

> [!IMPORTANT]
> Install from the `https://….ts.net` URL, never from `http://<ip>:8000`.
> The two icons look identical, but they are different origins with separate
> storage. An icon saved from the HTTP address never works offline, and songs
> kept on the device under one origin don't show up under the other. Step 4
> removes the HTTP address, so this mistake can't happen.

> [!WARNING]
> Pick the URL once and keep it. Renaming the machine or the tailnet changes
> the origin, which means you add the app to the home screen again and lose
> every song kept on the device.

To stop serving, run `sudo tailscale serve reset`.

If you want the app public without running a reverse proxy,
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) does that. Read the
section below first.

### On the public internet

Put the app behind a reverse proxy that handles HTTPS, such as
[Caddy](https://caddyserver.com), nginx or Traefik. With Caddy, which fetches
the certificate by itself:

```caddyfile
spotea.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Set the same `HOST_PORT=127.0.0.1:8000` and `SESSION_HTTPS_ONLY=true` as in
step 4 above.

> [!CAUTION]
> Registration is open to anyone who can reach the instance. Passwords are
> hashed and every account's data is kept separate. But accounts are just a
> username and a password, with no email or other check behind them, and only
> the login form is rate limited. On a public URL, anyone who finds it can
> create an account and download to your disk.

## Running without Docker

Requires Python 3.12+ and `ffmpeg`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# The defaults point at /app/data, the Docker path. Set relative ones instead:
#   DATABASE_URL=sqlite:///./data/spotea.db
#   STORAGE_DIR=./data/storage
#   AVATARS_DIR=./data/avatars
#   THUMBNAILS_DIR=./data/thumbnails

uvicorn app.main:app --reload
```

## Configuration

Every setting is an environment variable, read from `.env` if one exists.
None of them is required.

| Variable | Default | Description |
|---|---|---|
| `HOST_PORT` | `8000` | Port Docker publishes the app on. `127.0.0.1:8000` limits it to the machine itself (Docker only) |
| `SESSION_HTTPS_ONLY` | `false` | Sends the login cookie over HTTPS only. Turn it on once you use an HTTPS URL; with it on, logging in over plain HTTP silently fails |
| `SECRET_KEY` | generated | Key that signs login sessions. When unset, one is generated on first start and kept in `secret_key` next to `STORAGE_DIR` |
| `UPDATE_CHECK` | `true` | Whether the feature exists at all: asking GitHub once every 12 hours whether a newer Spotea was released, shown in Settings to the first account registered. `false` removes it, including its own Settings toggle. Leaving it `true` still lets that toggle turn the check off at runtime |
| `MUSIC_CHART_COUNTRIES` | `US,GB,CA,AU,IE,NZ` | Countries for Explore's Charts shelf, comma separated. Each one adds its "Trending 20" playlist. `ZZ` is YouTube Music's global chart, which is weighted by market size |
| `AUDIO_FORMAT` | `m4a` | Format yt-dlp saves audio in. `m4a` is YouTube's own stream, so saving it needs no conversion; other formats like `mp3` have to be converted and take longer |
| `DATABASE_URL` | `sqlite:////app/data/spotea.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | `/app/data/storage` | Downloaded audio, one folder per account. Two accounts with the same track each keep their own copy, so neither can delete the other's |
| `AVATARS_DIR` | `/app/data/avatars` | Cached artist avatars |
| `THUMBNAILS_DIR` | `/app/data/thumbnails` | Cached song and album artwork |
