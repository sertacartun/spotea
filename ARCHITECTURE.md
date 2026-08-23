# Spotea — Architecture

A self-hosted music player over YouTube Music. One login owns one library of
followed artists; opening the app notices what they release; yt-dlp turns a
track into a file on disk; the browser plays it.

This document is the map. The reasoning behind individual decisions lives in
the code comments, which are the primary source — this file says what the
pieces are and how they fit, not why each line is the way it is.

---

## 1. Component diagram

```
                 ┌──────────────────────────────────────────┐
   browser ────▶ │  FastAPI (app/main.py)                   │
                 │                                          │
                 │  routers/    HTTP surface                │
                 │  services/   follow, sync, recommend     │
                 │  youtube/    the YouTube Music client    │
                 │  downloader  yt-dlp, audio only          │
                 └───────┬──────────────────┬───────────────┘
                         │                  │
                 ┌───────▼───────┐  ┌───────▼────────────────┐
                 │ SQLite        │  │ ./data                 │
                 │ users         │  │   storage/  *.m4a      │
                 │ artists       │  │   avatars/  *.jpg      │
                 │ content       │  │   thumbnails/ *.jpg    │
                 └───────────────┘  └────────────────────────┘

   outbound:  music.youtube.com  (ytmusicapi — everything except audio)
              youtube.com        (yt-dlp — audio extraction only)
```

Two outbound dependencies, each with exactly one job. `ytmusicapi` answers
every question about music: search, artists, releases, playlists, charts,
moods. `yt-dlp` is imported in exactly one module — `app/downloader.py` — and
only ever to turn a video id into an audio file.

---

## 2. Project structure

```
app/
  main.py            app wiring, lifespan, image routes, image proxy
  models.py          User, Artist, Content, RecommendationCache
  schemas.py         request/response shapes
  database.py        engine, session, SQLite pragmas
  deps.py            get_db, get_current_user, require_login
  auth.py            password hashing, session key
  config.py          env-backed settings
  middleware.py      selective gzip, security headers
  scheduler.py       background loop: sweep disk
  services/refresh.py  when opening the app goes and looks for new releases
  storage.py         disk accounting, purge, orphan sweeps, export
  downloader.py      yt-dlp audio extraction — the only yt-dlp importer
  images.py          avatar/thumbnail fetch + cache, /image-proxy helpers (songs and releases too)
  content_query.py   one place that decides what a filter/playlist means
  page_context.py    the context every page and fragment renders from
  progress.py        expiring in-memory registries (downloads, syncs)
  interests.py       the free-text interest list's format
  timeutil.py        naive-UTC helpers
  formatting.py      filename/size/duration formatting
  templating.py      Jinja environment

  routers/
    pages.py         GET / — the whole app is one document
    partials.py      /partials/* — fragment re-renders of one region
    artists.py       follow, unfollow, refresh, which are still syncing
    explore.py       search, and turning a remote row into a playable one
    content.py       download, stream, favorite, save, queues
    recommendations.py  the Explore batch (GET/POST)
    settings.py      audio quality, interests, refresh interval
    storage.py       clear all, export zip
    auth.py          register, login, logout
    debug.py         playback breadcrumbs the server can't otherwise see

  services/
    artist_follow.py   "is this an artist?" — the one throat every follow goes through
    artist_sync.py     release-snapshot diff; the only writer of new Content
    initial_sync.py    the background first sync, and its progress registry
    remote_detail.py   artist / release / playlist panels
    recommendations.py the Explore batch, its cache and its TTL

  youtube/
    music.py         the ytmusicapi client — search, artist, release, charts, moods
    models.py        the result shapes routers and templates speak
    urls.py          id regexes, URL builders, thumbnail sizing

  templates/         index.html is the app; _*.html are its regions
  static/js/         ES modules, no build step
  static/css/        one stylesheet

tests/               pytest; every network call monkeypatched out
scripts/backup.sh    sqlite backup helper
```

There is no build step and no JS test runner. `tests/test_static_js.py`
guards the shipped JavaScript at source level instead — it pins mistakes
that have already been made once, and says so rather than pretending to
execute anything.

---

## 3. Data model

### `users`

One login, one library. Was two tables (`accounts` + household `users`
profiles); the profile model is gone.

| column | notes |
|---|---|
| `email`, `password_hash` | email lowercased at the router; unique |
| `audio_quality` | `high` / `low`, both remux rather than re-encode |
| `interests` | newline-separated free text; owned by `app/interests.py` |
| `refresh_interval_minutes` | 15 / 30 / 60 / 120 — a floor between checks, not a background clock |
| `refreshed_at` | NULL means never, which counts as due |

### `artists`

| column | notes |
|---|---|
| `channel_id` | the artist's "<Artist> - Topic" channel. **The key** — it is what a track carries, so a song grabbed from Explore lands on the same row as a deliberate follow |
| `browse_id` | how YouTube Music addresses their page; opens their profile, and what the sync asks about. NULL only on placeholder rows |
| `name`, `avatar_url` | display, filled in by the first sync |
| `followed` | False for a placeholder created to hold one Explore track, and for an artist unfollowed while keeping some of their content |
| `release_snapshot` | JSON array of every release the page listed last time — browse id, title, year, kind, cover. The change-detection mechanism *and* what both **New releases** surfaces render from. NULL means never synced. Tolerates the bare-id shape it used to hold; see `snapshot_release_ids` |
| `monthly_listeners` | YouTube Music's own count string ("1.91M"), refreshed every sync |
| `related_artists` | JSON array of their "fans also like" artists, refreshed every sync — feeds Explore's **Artists you may like** shelf |
| `top_tracks` | JSON array of their page-preview songs, refreshed every sync — feeds Explore's **Songs** shelf |

The last three cost nothing extra: they arrive on the same `get_artist` call
the sync already makes for every followed artist, and are simply kept
instead of discarded. See "Explore & recommendations" below.

### `content`

One track. `artist_id` + `user_id`, `video_id` unique per user, plus the
download state (`status`, `file_path`, `file_size_bytes`, `is_unavailable`),
the engagement flags (`is_favorite`, `last_played_at`) and two that decide
where a row shows up:

- `is_preview` — added from Explore and not favorited yet. Plays normally,
  stays out of Library, swept after 7 days.

`is_new_upload` used to sit alongside it, flagging tracks the sync inserted
so that a "New releases" shelf could show them. It is gone: that shelf only
ever held releases from *after* the follow and expired them after fourteen
days, so on a real library it was three tracks with a fortnight to live.
Both surfaces of that name read `release_snapshot` now.

Indexes are not cosmetic: measured on a 30k-row library they took the ten
hottest queries from 81.7ms of SQLite time to 3.8ms. See the comments on
`Content.__table_args__`.

### `recommendation_cache`

One row: the last Explore batch, keyed by a hash of the interest list plus a
payload version. Bumping `PAYLOAD_VERSION` invalidates every stored batch
without a migration.

### `track_lyrics`

One row per recording (keyed by `video_id`, not by a Content row — the same
song is several rows across previews, syncs and users). `lines` holds the
timed lyrics as JSON, and NULL means "asked YouTube, there are none", which
is different from having no row at all. See §7's player-panel notes for why
the negative answer is worth storing.

### Schema changes

There is no migration framework, and there should not be one.
`Base.metadata.create_all()` builds a fresh database. The framework that
existed was deleted along with the tables it patched forward.

One asymmetry is worth knowing, because it decides designs: `create_all`
does add a missing **table** to an existing database, but never a missing
**column**. So a new table is free and a new column is not — which is why
`track_lyrics` is a table rather than two columns on `content`.

Two guarded one-offs in `main.py`'s lifespan close the gap for `content`,
and between them they are the whole of what this app does about schema
change:

- `_drop_removed_columns()` drops a fixed list of columns whose features are
  gone. They were NOT NULL with no default, so leaving them meant an old
  database rejecting every INSERT.
- `_add_missing_columns()` adds a fixed list of **nullable** columns with
  `ALTER TABLE ADD COLUMN`. Without it the model and an existing file
  disagree and every SELECT against `content` fails with "no such column".

Both are idempotent, both no-op on a database `create_all` just built, and
neither renames, retypes, reorders or backfills anything. "A schema change
means a fresh database" was true while every change was a removal; for a
self-hosted app whose entire state is one SQLite file, telling the owner to
discard it is not an upgrade path. Anything needing a rename, a type change
or a backfill is a real migration and wants a considered decision, not
another entry in one of these tuples.

---

## 4. The two syncs

### Following an artist

`POST /artists` with a channel URL. `services/artist_follow.py` extracts the
channel id, asks YouTube Music who that is, and refuses the follow if the
answer isn't an artist — the music-only scope in one rule. It stores the
Topic channel as the key and the page's own browse id as the address (which
is not always the id that was asked for: a VEVO container redirects to the
page that actually has the music).

The route answers as soon as the row exists. The first sync runs as a
background task and Library's card reports on it (`data-preparing`, polled
against `GET /artists/syncing`).

### Noticing a release

`services/artist_sync.py`, triggered two ways: opening the app when the
library is due (`services/refresh.py`, queued behind the response so the page
never waits — that render shows what was already stored, the next one shows
what arrived), and the Refresh button, which ignores the interval and fetches
straight away.

There is no background refresh loop. There was one, ticking every five
minutes whether or not anyone was using the app; with the feed gone there is
nothing that goes stale while the tab is closed, so a 150-artist library was
being fetched around the clock to keep a page nobody was looking at correct.
`scheduler.py` still runs, for the disk and row sweeps that genuinely belong
on a clock.
For each followed artist: read their page, take albums + singles, diff the
release ids against `release_snapshot`, open each genuinely new release for
its tracks, insert them. The whole release — title, year, kind, cover — is
written back to the snapshot, not just its id: the page hands all of it over
in this same response, and keeping it is what lets both **New releases**
surfaces render without a request of their own.

Those two surfaces are the same data at two sizes: Home's shelf shows twelve
and links to Library's tile, which shows all of them. Both are **this
calendar year only**, because the year is the only date YouTube Music
publishes — measured across both responses that could carry one, there is no
month, day, timestamp or ISO date anywhere. One caveat comes with that and
cannot be worked around: `year` is the year of *this listing*, so a reissue
of a 1969 album reports the current year.

Neither has a Play all. A release carries no video ids until it is opened, so
playing a page of them would be one live request each; open one and it has
its own. A release holding exactly one track skips the panel and plays (§7).

Measured live per artist per refresh: **0.38–0.76s** for the page, plus
**0.09–0.20s** per new release — and usually there is no new release at all.

A first sync records the snapshot and imports nothing. Following means "tell
me what they put out from now on"; the back catalogue is a click away on
their profile.

**What this replaced.** An RSS read of the Topic channel, plus a yt-dlp call
per channel to find out how long anything was, plus a Shorts filter. What it
gives up is the exact publish timestamp — YouTube Music reports a year and
nothing finer, so a new release is stamped with when it was first seen, which
on a 30-minute interval is within half an hour of the truth. What it gains:
durations and cover art arrive with the tracks, and a guest verse on someone
else's record is caught, which never reaches the artist's own Topic channel
at all.

---

## 5. Explore & recommendations

`services/recommendations.py` builds one batch of shelves per user, cached in
`recommendation_cache` and rebuilt when it goes stale, the interest list
changes, or the Refresh button is pressed (`GET/POST /recommendations`,
`/recommendations/refresh`). Six shelves, three different sources:

| shelf | source | cost |
|---|---|---|
| **Playlists** | YouTube Music playlist search, keyed on a sample of the typed interest list | a live search per sampled interest |
| **Songs** | every followed artist's own `top_tracks` (§3), interleaved a position at a time and deduped | none — already on disk |
| **Artists you may like** | every followed artist's own `related_artists` (§3), interleaved a position at a time and deduped | none — already on disk |
| **Charts** / **Charting artists** | each `MUSIC_CHART_COUNTRIES` entry's "Trending 20" playlist, plus its charting artists blended a rank at a time | one live request per country, shared by both |
| **Moods** (listed first) | every category in YouTube Music's "Moods & moments" menu | one live request, playlists not fetched until a mood is opened |

Songs and Artists you may like used to both be interest-driven search too, the
same way Playlists still is. Both were dropped: an interest is free text,
and it was routinely a genre or a mood rather than a song title or an
artist's name ("Hip Hop", not "Drake") — YouTube Music's artist search in
particular answers that kind of query with beatmaker/compilation channels,
not real artists (measured live). Rebuilding both from data already sitting
on a followed artist's own row is both cheaper and more reliable than typing
never was.

The two follow-based shelves are computed fresh on every read regardless of
the batch's own cache state (`recommendations._merge_from_followed`) — they
cost a query over local data, not a network call, so there's no reason to
let them go stale with the rest of the batch. A profile with nothing
followed gets neither shelf at all, with no seeded default.

**Opening a mood.** Clicking one in "Moods" opens
`GET /partials/detail/yt-mood/{params}` — a panel of that mood's playlists
(`templates/_mood_panel.html`, one more live request, paid only for the
mood actually opened), rendered as a shelf of cards rather than a track
list. Picking a playlist from there opens it the ordinary
`yt-playlist` way. Only "Moods & moments" is listed, not YouTube Music's
"Genres" menu — see §9.

---

## 6. Endpoints

| method | path | what |
|---|---|---|
| GET | `/` | the whole app, one document |
| GET | `/partials/{home,library,downloads,storage-summary}` | re-render one region |
| GET | `/partials/detail/playlist/{kind}` | a pinned playlist |
| GET | `/partials/detail/yt-artist/{browse_id}` | an artist's profile |
| GET | `/partials/detail/yt-artist-songs/{browse_id}` | their whole song list |
| GET | `/partials/detail/yt-release/{browse_id}` | an album's panel, **or** a one-track release's track as JSON (see below) |
| GET | `/partials/detail/yt-playlist/{playlist_id}` | a YouTube Music playlist |
| GET | `/partials/detail/yt-mood/{params}` | a mood's playlists |
| POST/DELETE | `/artists`, `/artists/{id}` | follow, unfollow |
| POST | `/artists/refresh` | sync now |
| GET | `/artists/syncing` | which are still filling in |
| GET | `/explore/artists`, `/explore/songs` | search |
| POST | `/explore/tracks`, `/explore/tracks/batch` | make a remote row playable |
| DELETE | `/explore/tracks/{content_id}` | drop a preview |
| POST/GET | `/content/{id}/download`, `/status` | fetch audio, poll it |
| GET | `/content/{id}/stream` | play it (Range-capable FileResponse) |
| GET | `/content/queue/playlist/{kind}` | the ids behind a Play all |
| POST/DELETE | `/content/{id}/{favorite,save}` | engagement flags |
| GET/PUT | `/settings` | quality, interests, interval |
| GET/POST | `/recommendations`, `/recommendations/refresh` | the Explore batch |
| DELETE/GET | `/storage`, `/storage/export` | clear all, zip |
| GET | `/avatars/*`, `/thumbnails/*`, `/image-proxy` | images |
| POST | `/register`, `/login`, `/logout` | auth |
| GET | `/health` | db + sweep-loop liveness |

Everything except auth and `/health` requires a session.

---

## 7. The client

`index.html` is the entire app: Home, Library, Explore, Settings and the
detail panel are tab panels in one document, routed by the URL hash. An
inline head script paints the right tab before any module loads, so a reload
never flashes the wrong one.

Two mechanisms carry almost all of the interactivity:

- **Fragment refresh.** Anything that changes what a region shows re-fetches
  that region from `/partials/*` and swaps it in (`static/js/fragments.js`).
  Nothing hand-patches the DOM; a shelf and its count can't disagree.
- **The detail panel.** One panel, three body shapes: a track list
  (a pinned playlist, or remote `yt-playlist`/`yt-artist-songs`/`yt-release`),
  an artist profile (`yt-artist`, shelves rather than a list), or a mood's
  playlist shelf (`yt-mood`). Remote fragments are cached per URL for the
  session, since nothing this app does changes what YouTube Music would
  answer.

`yt-release` is the one route with two response shapes. A release holding
exactly one track answers with that track as JSON instead of a panel, and
the client plays it rather than opening anything — a one-track panel was a
cover, a title and a single row, which is a page whose only content is a
button to do the thing you already asked for. The rule is the track count,
not YouTube Music's own "Single" label: it puts that label on two- and
three-track releases too, and going by it would make the other tracks
unreachable. Deciding costs nothing extra, because the count and the video
id arrive in the same fetch.

Playing anything that didn't come out of a list — that single, an Explore
result, one of an artist's videos — sets a queue of exactly that one track
(`home/remote.js`). Not an empty one: `noteCurrent` drops any queue the
current track isn't in, so without this the queue panel would go blank while
something plays.

### The player panel

The panel beside (or under) the player has two tabs, Queue and Lyrics.

Below 900px it is a drawer: the artwork gives up height, the panel grows
into it, and a downward drag closes it. At 900px and up it simply lives in
the other half of the card, open from the moment the player is, and the
toggle that opened it is hidden. One mechanism, not two — `setQueueOpen`
forces "open" at that width, so `is-open` still means "this panel is
showing" and the existing load/refresh paths work unchanged. The breakpoint
is asserted equal in JS and CSS by a test, because a drift there means a
panel that loads invisibly or shows empty.

**Lyrics are fetched only when their tab is opened**, never on play, and
cached in `track_lyrics` keyed by `video_id`. Both halves of that are
measured rather than assumed: a miss costs two live YouTube requests
(`get_watch_playlist` for the `MPLYt…` browse id, then `get_lyrics`), and of
21 tracks sampled, 6 had timed lyrics, **0** had untimed ones, and the rest
had none. So there is no timed-else-plain ladder — a track has timed lyrics
or it has nothing — and the *absence* is cached as firmly as a hit (`lines`
NULL means "asked, there are none"), because that is the common answer.

Following along is a `timeupdate` consumer and touches nothing else about
the audio element. A test pins that: an unrelated module calling `pause()`
is the exact shape of the iOS background-playback bug this codebase already
paid for once.

The player is a single `<audio>` element. It is the only one, deliberately:
adding a second to solve an iOS background-playback problem is what caused
the problem the second element was added to fix.

Track handoffs on that element run at two different moments. In the
foreground, a track plays to `ended` and the handler advances the queue. In
the background, `ended` is a cliff: iOS starts freezing the page the moment
nothing renders (measured 2026-08-23 — a handoff with the next track's bytes
already in memory and `play()` already called still sat unstarted until the
screen woke), so `home/overlay.js` swaps to the next track just before the
end, while audio is still rendering and the page still provably holds the
audio session — and only when the successor's bytes were prefetched into
memory, since a swap that still needs the network trades one stall for
another. Everything the early handoff declines falls back to the `ended`
path unchanged.

---

## 8. Concurrency & security

- **One worker, always.** In-memory registries (download progress, sync
  progress, login-failure counts) are process-local, and the app asserts a
  single worker at startup rather than silently misbehaving under two.
- **SQLite in WAL mode** with foreign keys on. Fetches fan out across a small
  thread pool; DB writes never happen inside it.
- **Sessions** are signed cookies (`itsdangerous`). Passwords are bcrypt, and
  a login for an unknown email still pays a real bcrypt check so timing
  doesn't reveal which emails are registered.
- **Login is rate limited** per client, because each attempt costs ~420ms of
  a shared threadpool worker.
- **Every outbound URL is host-checked** before it is fetched, so an
  authenticated user can't point a follow at `127.0.0.1`.
- **The image proxy** (avatars, song/album/playlist covers alike) only
  fetches from an allowlist of Google/YouTube image hosts, and answers
  anything it can't fetch with a transparent pixel rather than a broken
  image.
- **CSP and security headers** are set in `app/middleware.py`; gzip is
  applied selectively, never to audio or images.

---

## 9. Known failure modes

**YouTube refusing a download (HTTP 403).** The retry ladder in
`downloader.py` walks client impersonations. A track that every client
refuses is marked `is_unavailable` and skipped instantly on the next play
rather than spending an extraction to be told the same thing — usually a
Topic-channel track licensed for other countries. Deleting the download
clears the flag, which is the manual "try again".

**ytmusicapi is unofficial.** Its playlist parser already fails on 25 of
YouTube Music's 40 mood/genre categories — every one of them under "Genres",
plus one mood — measured live, which is why the shelf is called "Moods" and lists only the
"Moods & moments" section (`youtube/music.py`'s `MOOD_SECTION`); the other
section would 500 the moment one of those categories was opened. A parser
break takes out discovery; it cannot take out the library, because playback
is a local file and a local row.

**Release detection lag.** The sync sees a release when the artist's page
lists it. How quickly that happens after a real release is not measured.

---

## 10. Deployment

```bash
cp .env.example .env      # set SECRET_KEY
docker compose up -d --build
```

`./data` holds the database, the audio, and the image caches, and survives
rebuilds. `yt-dlp` and `ytmusicapi` are installed in their own Docker layer
after `COPY app`, so any real change to the app picks up current versions of
both — pinning them in `requirements.txt` meant the running image sat six
weeks behind upstream while looking, from the file, like it floated.

Runtime dependencies: FastAPI, uvicorn, SQLAlchemy, pydantic-settings,
Jinja2, python-multipart, itsdangerous, bcrypt — plus yt-dlp and ytmusicapi
from the Dockerfile layer. `ffmpeg` for remuxing.

---

## 11. Verifying UI work

The test suite covers the server and guards the shipped JS at source level,
but it cannot click anything. Anything that changes what a person sees is
verified by running the real app:

```bash
docker compose up -d --build
curl -s localhost:${HOST_PORT:-8000}/health
```

then walking the round it exists to serve: register, search an artist, follow
them, add a song, download it, play it.
