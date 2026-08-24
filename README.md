# Spotea

A self-hosted music player built on YouTube Music. Follow artists, get told
when they release something, download what you want to keep, and listen to it
from your browser — a personal, free alternative to a streaming subscription.

- Register your own account and log in with it. One login, one library.
- **Explore** searches YouTube Music for artists, songs and ready-made
  playlists — the music catalogue, not youtube.com, so a search for an
  artist returns their tracks rather than reaction videos and compilations.
- Follow an artist and their releases start arriving. Opening the app checks
  what YouTube Music lists for your artists against what it listed last time,
  and Home's **New releases** shelf shows what they've put out — read from
  what that check already stored, so opening Home never waits on the network.
  The check runs behind the page rather than in front of it, and no more
  often than the interval in Settings; the Refresh button ignores that and
  looks straight away. Only artists can be followed — this app holds music
  and nothing else.
- A brand new library asks what you listen to and fills Explore from the
  answer, rather than handing you an empty page.
- Open an artist and you get their page: popular songs, albums, singles and
  similar artists. Open an album or single and you get its tracks, with real
  durations and cover art.
- Play anything without following anyone: add a single song straight from
  search and it plays like everything else.
- Explore fills up on its own once you follow a few artists: **Songs** and
  **Artists you may like** come from who you actually follow, not from typing
  anything. List genres, artists or moods under **Interests** in Settings
  and that drives the **Playlists** shelf too. This week's charts and every
  one of YouTube Music's moods to browse are there regardless.
- Downloads are yours: audio is extracted with yt-dlp, stored on your own
  disk, and exported as one zip whenever you want it.
- **Keep songs on the device.** The Downloads list has a phone button on
  every row: press it and that track's audio is copied into the browser
  itself, cover art included. It then plays with no network at all — on a
  train, on a plane, or anywhere the instance simply isn't reachable — and
  keeps playing even if you later clear the server's own copy. The line above
  the list says how much of the device's storage the copies use, and removes
  them all in one go. How much a browser will let you keep varies (iOS is
  both the smallest and the least predictable); the app asks for persistent
  storage the first time you keep something, and tells you when it's full.
- **It opens with no connection.** Installed to a home screen, the app
  precaches itself, so opening it offline gives you Home and Library as the
  server last rendered them rather than the browser's "no internet" page — and
  anything you kept plays straight from there. A bar across the top says when
  you're offline; the Downloads list is still the place to see exactly what
  the device is holding. Tapping something you didn't keep says so instead of
  failing quietly.

## Running with Docker (recommended)

1. Copy the example env file and fill in real values:

   ```bash
   cp .env.example .env
   ```

   Generate a `SECRET_KEY`:

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Start the app:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:8000` (or whatever `HOST_PORT` you set in `.env`)
   and register an account — registration is open to anyone who can reach
   the instance, so keep that in mind if you expose it publicly (see below).

Downloaded audio and the SQLite database persist in `./data` on the host,
so they survive container restarts and rebuilds.

### Updating

```bash
git pull
docker compose up -d --build
```

Your `./data` volume is untouched by rebuilds.

Save-for-later was removed, and its column goes with it: the first start
after the update drops `content.is_saved` automatically, because leaving it
would stop the app adding any track at all. Nothing you had saved is carried
over anywhere — the feature is gone, not moved. Run `./scripts/backup.sh`
first if you want a copy of the old shape.

Upgrading across the music-only rewrite needs a fresh database: the schema
changed shape (feeds became artists, profiles were folded into the account)
and no migration path was written for it. Move `./data/spotea.db` aside,
start the app, and register again.

### Exposing it beyond your local network

Login is real per-account authentication (hashed passwords, isolated data
per account) — but there's no email verification, and only login is rate
limited. If you expose
this instance to the internet, put it behind a reverse proxy with HTTPS
(e.g. Caddy, nginx, Traefik), and consider whether you want registration
open to anyone who finds the URL.

## Running locally without Docker

Requires Python 3.12+ and `ffmpeg` installed on your system.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — for local dev, relative paths work well, e.g.:
#   DATABASE_URL=sqlite:///./data/spotea.db
#   STORAGE_DIR=./data/storage

uvicorn app.main:app --reload
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | yes | — | Random key used to sign session cookies |
| `DATABASE_URL` | no | `sqlite:////app/data/spotea.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | no | `/app/data/storage` | Where downloaded audio files are stored, in a subdirectory per account — the file name is the video id alone, so two accounts with the same track need two copies rather than one file either of them could delete out from under the other |
| `AVATARS_DIR` | no | `/app/data/avatars` | Where fetched artist avatars are stored |
| `THUMBNAILS_DIR` | no | `/app/data/thumbnails` | Where cached song/album thumbnails are stored |
| `AUDIO_FORMAT` | no | `m4a` | Audio format yt-dlp extracts to |
| `SESSION_HTTPS_ONLY` | no | `false` | Marks the session cookie Secure — turn on once the app sits behind HTTPS |
| `MUSIC_CHART_COUNTRIES` | no | `US,GB,CA,AU,IE,NZ` | Country codes for Explore's Charts shelf, comma separated. Each contributes one tile — its "Trending 20" playlist; the video charts alongside it are dropped. `ZZ` is YouTube Music's global chart, which is weighted by market size; naming countries is the only way to change that mix |
| `HOST_PORT` | no | `8000` | Host port docker-compose publishes the app on (Docker only) |

## License

MIT — see [LICENSE](LICENSE).
