# Self-hosting Spotea

The [README](../README.md) gets you running in three steps. This page covers
everything else.

## Running with Docker

1. Copy the example env file and set a `SECRET_KEY`:

   ```bash
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Start the app:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:8000` (or whatever `HOST_PORT` you set in `.env`)
   and register an account.

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

## Exposing it beyond your local network

Registration is open to anyone who can reach the instance. Login is real
per-account authentication (hashed passwords, isolated data per account) —
but accounts are a username and a password with nothing to verify them
against, and only login is rate limited. If you expose this instance to the
internet, put it behind a reverse proxy with HTTPS (e.g. Caddy, nginx,
Traefik), and consider whether you want registration open to anyone who
finds the URL.

Offline playback and installing to a home screen need HTTPS too: browsers
only register the service worker on a secure origin (plain `http://` works
on `localhost` and nowhere else).

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
