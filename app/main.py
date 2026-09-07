import base64
import logging
import os
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated, require_login
from app.images import fetch_image_bytes
from app.middleware import install as install_middleware
from app.routers import artists as artists_router
from app.routers import auth as auth_router
from app.routers import content as content_router
from app.routers import debug as debug_router
from app.routers import explore as explore_router
from app.routers import pages as pages_router
from app.routers import partials as partials_router
from app.routers import playlists as playlists_router
from app.routers import recommendations as recommendations_router
from app.routers import settings as settings_router
from app.routers import storage as storage_router
from app.storage import sweep_startup_leftovers

# uvicorn configures only its own loggers, leaving the root at WARNING, so
# every logger.info() this app makes went nowhere. That's not cosmetic: the
# download ladder logged which attempt failed and why at INFO, and a day of
# "why does it always take three tries?" was spent without that line ever
# reaching a handler. uvicorn's own loggers don't propagate, so this doesn't
# duplicate the access log.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

logger = logging.getLogger(__name__)


def _assert_single_worker() -> None:
    """Refuse to start under more than one uvicorn worker.

    Download/backfill/import progress (app/progress.py's ProgressRegistry)
    and the recommendations build lock (app/services/recommendations.py) are
    in-process, module-level state — a second worker is a second OS process
    with its own copy, so polling clients would see progress silently go
    missing or the build lock stop serializing runs. See the Dockerfile CMD
    comment for the fuller version of this.

    This can only catch the WEB_CONCURRENCY env-var route to multiple
    workers, not an explicit `--workers N` added to the CMD by hand: uvicorn
    reads WEB_CONCURRENCY exactly like --workers whenever --workers itself is
    omitted (true of this image's CMD), and that env var is still visible via
    os.environ in each worker uvicorn spawns — but --workers passed directly
    on the command line leaves no trace an in-process check can see (uvicorn
    spawns workers via multiprocessing's "spawn" context, and the child
    doesn't inherit the parent's sys.argv). That gap is real and is
    intentionally left to the Dockerfile comment rather than something
    invented here.
    """
    concurrency = os.environ.get("WEB_CONCURRENCY")
    if concurrency is not None and int(concurrency) != 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={concurrency!r} — Spotea must run with exactly "
            "one worker (unset WEB_CONCURRENCY or set it to 1). See "
            "app/progress.py and the Dockerfile CMD comment for why."
        )


# Columns removed along with the features that wrote them, as
# (table, column, index that referenced it or None). Dropped at startup
# rather than left in place, because every one of these is NOT NULL with no
# default: with the writer gone, an old database rejects every INSERT into
# that table — no track added from Explore, no release picked up by a sync,
# no account registered.
_REMOVED_COLUMNS = (
    # Save-for-later, removed in #23.
    ("content", "is_saved", "ix_content_user_saved"),
    # "New releases" was a shelf of Content rows flagged this way — releases
    # that appeared after the follow, expiring after fourteen days. Both
    # surfaces of that name read Artist.release_snapshot now, which has
    # neither limit.
    ("content", "is_new_upload", "ix_content_user_newupload"),
    # How long to wait before checking followed artists again. There is no
    # interval any more: a library is checked once, when its owner first
    # opens the app, and after that only when Refresh is pressed (see
    # services/refresh.py).
    ("users", "refresh_interval_minutes", None),
)


def _drop_removed_columns() -> None:
    """Brings an older database up to the columns this version expects.

    ARCHITECTURE.md says there is no migration framework and a schema change
    means a fresh database. That stays true, and this is not one: it drops a
    fixed list of columns whose features are gone and does nothing else. It
    cannot add a column or backfill anything.

    It exists because these particular changes cannot be left to the user —
    see _REMOVED_COLUMNS. An app that silently can't add music is worse than
    one that spends a few milliseconds at startup checking a PRAGMA.

    Not a script, because a script has nowhere to run in this deployment: the
    image carries no sqlite3 CLI and no scripts/ directory, and on the host
    the database file belongs to the container's user, not the operator's.
    Here it runs as the right user, inside the container, exactly once.

    A no-op on every start after the first, and on any database `create_all`
    just built.
    """
    dropped: list[str] = []
    with engine.begin() as conn:
        for table, column, index in _REMOVED_COLUMNS:
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in present:
                continue
            # DROP COLUMN refuses while an index still references the column,
            # so the index goes first. All of it inside engine.begin()'s
            # transaction: the app expects either shape, never a half-applied
            # one.
            try:
                if index is not None:
                    conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index}")
                conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
            except Exception as exc:  # pragma: no cover - depends on the SQLite build
                raise RuntimeError(
                    f"Could not drop the obsolete {table}.{column} column, which "
                    "this version of Spotea needs gone before it can write to "
                    f"{table} (DROP COLUMN needs SQLite 3.35 or newer). Back up "
                    "./data/spotea.db, then either upgrade SQLite or start from a "
                    "fresh database."
                ) from exc
            dropped.append(f"{table}.{column}")

    if dropped:
        logger.info("Dropped obsolete columns: %s", ", ".join(dropped))


# Columns added to `content` since the first release, each with the DDL type
# to create it as. Nullable with no default, every one of them: `create_all`
# builds these on a fresh database and this adds them to an existing one, and
# the two have to agree on a shape that needs no backfill to be correct.
_ADDED_CONTENT_COLUMNS = (
    # Who this particular recording is credited to. Previously the Artist
    # row's name, which is shared by every track on that artist's channel —
    # see models.Content.artist_credit.
    ("artist_credit", "VARCHAR(300)"),
)


def _add_missing_columns() -> None:
    """The other half of _drop_removed_columns: columns this version expects
    that an older database doesn't have.

    That function's docstring says it cannot add a column, and ARCHITECTURE.md
    said a schema change means a fresh database. Both were written when the
    only schema changes were removals. They stop being reasonable the moment a
    change is additive: `create_all` adds tables to an existing database but
    never columns, so the model and the file disagree and every SELECT against
    `content` fails with "no such column" — for a self-hosted app whose whole
    state is one SQLite file the user cannot afford to discard, "start over"
    is not an upgrade path.

    Deliberately the narrowest thing that works, and not a migration
    framework: a fixed list of nullable columns, added with ALTER TABLE ADD
    COLUMN, in one transaction. No renames, no type changes, no backfills, no
    ordering, no version table. Anything that needs those is a real migration
    and should be a considered decision rather than something this grew into.

    A no-op on every start after the first, and on any database `create_all`
    just built.
    """
    with engine.begin() as conn:
        present = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(content)")}
        missing = [(c, t) for c, t in _ADDED_CONTENT_COLUMNS if c not in present]
        if not missing:
            return

        for column, ddl_type in missing:
            conn.exec_driver_sql(f"ALTER TABLE content ADD COLUMN {column} {ddl_type}")

    logger.info("Added missing content columns: %s", ", ".join(c for c, _ in missing))


def _rename_email_to_username() -> None:
    """The one rename this app has ever needed, and the reason the two
    functions above say they cannot do one.

    Logging in was by email address. Nothing was ever sent to it, so the
    field was already a username wearing an @; it is now a username outright
    (see models.User). Dropping the column and asking for a new name would
    have locked every existing account out of its own library, so the column
    is renamed in place — which SQLite does without rewriting the table, and
    which carries the unique index across with it.

    The values are then shortened to the part before the @, because
    "you@example.com" is a poor thing to have to type at a login prompt. That
    happens per row and only where it is safe:

      - never when two accounts would end up with the same name, since the
        column is unique and the UPDATE would fail — those rows keep the
        whole address, which still works as a username;
      - never when the part before the @ is empty.

    Nobody is locked out either way: whatever ends up in the column is what
    the login form now asks for, and the password is untouched.

    A no-op on every start after the first, and on any database `create_all`
    just built (which creates `username` directly and no `email` at all).
    """
    with engine.begin() as conn:
        present = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if "email" not in present or "username" in present:
            return

        conn.exec_driver_sql("ALTER TABLE users RENAME COLUMN email TO username")

        rows = list(conn.exec_driver_sql("SELECT id, username FROM users"))
        # Decided across the whole table before a single row is written, so
        # the outcome does not depend on which row is visited first: a local
        # part two accounts would both want is a name neither of them gets.
        wanted: dict[int, str] = {}
        for user_id, value in rows:
            local_part, _, _ = value.partition("@")
            if local_part:
                wanted[user_id] = local_part
        contested = {
            name
            for name in wanted.values()
            if list(wanted.values()).count(name) > 1
        } | {value for _, value in rows}

        shortened = 0
        for user_id, local_part in wanted.items():
            if local_part in contested:
                continue
            conn.exec_driver_sql(
                "UPDATE users SET username = ? WHERE id = ?", (local_part, user_id)
            )
            shortened += 1

    logger.info(
        "Renamed users.email to users.username (%d of %d shortened to the local part)",
        shortened,
        len(rows),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_single_worker()
    Base.metadata.create_all(bind=engine)
    _drop_removed_columns()
    _add_missing_columns()
    _rename_email_to_username()

    # Exactly once, here, before anything else in the process has had a
    # chance to start a download — see storage.sweep_startup_leftovers for
    # why this is the only moment a ".part" file is safe to delete
    # unconditionally.
    removed = sweep_startup_leftovers()
    if removed:
        logger.info("Removed %d abandoned .part file(s) from a previous run", removed)

    # Started/stopped through the scheduler module rather than held as a local
    # task here, so /health can ask it whether the loop is still alive.
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Spotea", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spotea_session",
    same_site="lax",
    # Off by default because the app is reachable over plain HTTP on a LAN,
    # which is how most installs run it; set SESSION_HTTPS_ONLY=true when it's
    # behind a TLS-terminating proxy so the session cookie can't leak over an
    # unencrypted hop.
    https_only=settings.session_https_only,
)

# Added after SessionMiddleware, so these run outside it — see middleware.install.
install_middleware(app)

class RevalidatingStaticFiles(StaticFiles):
    """Static files that must be revalidated before reuse.

    Starlette's StaticFiles sends ETag/Last-Modified but no Cache-Control, and
    browsers then fall back to *heuristic* freshness: they serve the cached copy
    for a while without asking the server at all. After an upgrade
    (`docker compose up -d --build`) that means users can keep running stale
    CSS/JS against new templates — which renders as a subtly (or completely)
    broken UI. "no-cache" still allows caching, it just forces a revalidation
    request; unchanged files come back as a cheap 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory="app/static"), name="static")

app.include_router(auth_router.router)
app.include_router(artists_router.router)
app.include_router(explore_router.router)
app.include_router(content_router.router)
app.include_router(playlists_router.router)
app.include_router(storage_router.router)
app.include_router(settings_router.router)
app.include_router(recommendations_router.router)
app.include_router(debug_router.router)
app.include_router(partials_router.router)
app.include_router(pages_router.router)


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
def health(response: Response) -> dict[str, object]:
    """Liveness that can actually fail.

    This used to return a hardcoded {"status": "ok"} — true whether or not the
    database was reachable and whether or not background refreshing had
    stopped. A probe that cannot fail is not a probe; it reported healthy
    through exactly the outage it existed to catch (see scheduler.run_scheduler).

    Deliberately cheap enough for a container healthcheck to poll: one
    `SELECT 1` and an in-process flag, touching no user data and doing no
    per-user work.
    """
    checks = {"database": _database_reachable(), "scheduler": scheduler.is_alive()}
    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", **checks}


def _database_reachable() -> bool:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Health check: database unreachable")
        return False


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from the root rather than under /static/js/ — a service worker
    # can only ever control paths at or below its own URL, so /static/js/sw.js
    # would default to a /static/js/ scope instead of the whole app. No
    # require_login: the browser's installability checks may fetch this
    # before there's a session to send.
    return FileResponse(
        "app/static/js/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# Both avatars and thumbnails are content-addressed (filename is the
# channel/video id, and download_avatar/download_thumbnail never overwrite
# an existing file — see their on-disk check) — so unlike RevalidatingStaticFiles'
# app code, a cached copy is never stale and can be trusted indefinitely
# without a revalidation round trip.
_IMAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/avatars/{filename}", dependencies=[Depends(require_login)])
def get_avatar(filename: str) -> FileResponse:
    # Downloaded and re-served from our own origin rather than hotlinked
    # from Google's CDN — see downloader.download_avatar for why. filename
    # is always "{channel_id}.jpg" from that function, but guard against
    # path traversal since it still arrives as attacker-controlled input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.avatars_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)


# Hosts youtube/urls.py's absolute_thumbnail_url ever hands back —
# yt3.googleusercontent.com is the one it rewrites away from (see that
# function), kept here too in case some path skips the rewrite.
#
# lh3.googleusercontent.com is the same CDN under a different name, and
# YouTube Music picks between the two with no pattern worth guessing at: of
# twelve charting artists, eight portraits came back on yt3 and four on lh3
# — and song/album covers split the same way. absolute_thumbnail_url's
# rewrite is deliberately host-and-path specific and doesn't cover lh3 (the
# two names don't share a path namespace, so rewriting would 404), which
# left anything on that host rejected by this proxy before ever fetching —
# or, hotlinked straight from the browser instead of through here, blocked
# outright by this app's own img-src CSP (app/middleware.py), which never
# allowed googleusercontent.com at all.
#
# i.ytimg.com is a third CDN entirely — YouTube's ordinary video-thumbnail
# host, not YouTube Music's cover art one. A mood/mix playlist's tracks
# (see youtube/music.py's fetch_mood_playlists, fetch_playlist) report their
# thumbnails there instead of on yt3/lh3, measured live on a mood shelf's
# "Fall Hits" playlist — every one of its 200 tracks. cover_url_at_size
# already knows this host can't be resized the way the other two can (its
# `sqp` query is signed), and passes it through untouched; this is the
# other half of supporting it, so those thumbnails get proxied rather than
# rejected before ever being fetched.
#
# Exact hostname match only: no endswith/substring check, which a URL like
# https://evil.example/yt3.ggpht.com could otherwise slip past.
_IMAGE_PROXY_ALLOWED_HOSTS = {
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "lh3.googleusercontent.com",
    "i.ytimg.com",
}
_IMAGE_PROXY_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}

# A 1x1 fully transparent PNG, served in place of an image whose upstream
# fetch failed — see image_proxy below for why that beats an error status.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# Deliberately not _IMAGE_PROXY_CACHE_HEADERS' day-long cache: this is a
# stand-in for a fetch that failed, and a transient upstream hiccup must not
# freeze a blank circle in the browser until tomorrow. Nothing is stored, so
# the next render retries.
_IMAGE_PROXY_BLANK_HEADERS = {"Cache-Control": "no-store"}


@app.get("/image-proxy", dependencies=[Depends(require_login)])
def image_proxy(u: str) -> Response:
    """Streams a channel avatar, or a song/album/playlist cover, from
    YouTube Music's own CDN without ever saving it to disk. A channel
    nobody's followed doesn't earn a permanent local copy (see
    download_avatar's own docstring on why not: 92% of avatar files on disk
    used to be exactly that, orphaned), and neither does a track or release
    cover — those are browsed far more often than anything is followed, so
    the same orphan problem would be worse. But hotlinking Google's CDN
    straight from the browser hits the ORB problem download_avatar exists
    to dodge (Chrome's Opaque Response Blocking rejects a real share of
    yt3.ggpht.com responses outright), and this app's own CSP blocks
    googleusercontent.com hosts entirely. This is the same fix as
    download_avatar, without the permanent copy: fetch once server-side per
    request, forward the bytes, keep nothing.

    `u` only ever reaches here as a URL this app generated itself (see
    images.cached_avatar_or_hotlink and youtube/music.py's
    _proxied_cover_url) — the host allowlist below is what keeps a tampered
    query string from turning this into an open fetch of arbitrary hosts on
    the server's behalf.

    A failed upstream fetch answers with a blank pixel rather than an error
    status. An <img> whose src 404s paints the browser's own broken-image
    glyph, and every avatar in the app renders through .search-result-thumb,
    which already carries the grey circle used for a channel with no avatar
    at all — so a transparent pixel lands in exactly that placeholder
    instead of a broken icon, with no markup or onerror handler anywhere.
    Explore's search results hit this path any time Google refuses a
    fetch."""
    host = urllib.parse.urlparse(u).hostname
    if host not in _IMAGE_PROXY_ALLOWED_HOSTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    # Fetched exactly as asked for, at the size YouTube Music named.
    #
    # This used to try `maxresdefault` first for a video still and fall back,
    # to sharpen the 400x225 covers a music-video playlist entry carries. It
    # worked and it is gone, because the reason for it is: a video row is now
    # swapped for its song before it plays (see routers/content.py's
    # swap_in_song_version), and the song's cover is square album art at
    # 544px — so the player, the one surface that drew a still large enough
    # for 400px to look soft, no longer draws one at all. What was left was
    # a list of 200px cards paying for 1280x720 frames: measured over 24 real
    # covers, 496 KB became 2151 KB, 4.3x the bytes to a phone, for artwork
    # that is already sharp at the size it is drawn.
    fetched = fetch_image_bytes(u)
    if fetched is None:
        # Logged rather than swallowed: turning every failure into a blank
        # pixel would otherwise make a genuinely broken proxy (bad egress,
        # DNS, an upstream host change) look like a page full of channels
        # that merely have no avatar.
        logger.warning("Image proxy could not fetch %s — serving a blank placeholder", u)
        return Response(
            content=_BLANK_PNG, media_type="image/png", headers=_IMAGE_PROXY_BLANK_HEADERS
        )

    body, content_type = fetched
    return Response(content=body, media_type=content_type, headers=_IMAGE_PROXY_CACHE_HEADERS)


@app.get("/thumbnails/{filename}", dependencies=[Depends(require_login)])
def get_thumbnail(filename: str) -> FileResponse:
    # Same deal as get_avatar above — see downloader.download_thumbnail.
    # filename is always "{video_id}.jpg" from that function, but guard
    # against path traversal since it still arrives as attacker-controlled
    # input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.thumbnails_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)
