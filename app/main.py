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
from app.config import resolve_secret_key, settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated, require_login
from app.images import fetch_image_bytes
from app.middleware import install as install_middleware
from app.routers import artists as artists_router
from app.routers import auth as auth_router
from app.routers import content as content_router
from app.routers import debug as debug_router
from app.routers import explore as explore_router
from app.routers import offline as offline_router
from app.routers import pages as pages_router
from app.routers import partials as partials_router
from app.routers import playlists as playlists_router
from app.routers import recommendations as recommendations_router
from app.routers import settings as settings_router
from app.routers import storage as storage_router
from app.storage import reset_interrupted_downloads, sweep_startup_leftovers

# uvicorn leaves the root logger at WARNING, which silently drops every app logger.info().
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

logger = logging.getLogger(__name__)


def _assert_single_worker() -> None:
    """Refuse to start under more than one worker: progress and the build lock are in-process state.

    Only WEB_CONCURRENCY is detectable here; an explicit `--workers N` leaves no trace in the child.
    """
    concurrency = os.environ.get("WEB_CONCURRENCY")
    if concurrency is not None and int(concurrency) != 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={concurrency!r} — Spotea must run with exactly "
            "one worker (unset WEB_CONCURRENCY or set it to 1). See "
            "app/progress.py and the Dockerfile CMD comment for why."
        )


# (table, column, referencing index). Must be dropped, not ignored: each is NOT NULL with no
# default, so with its writer gone an old database rejects every INSERT into that table.
_REMOVED_COLUMNS = (
    ("content", "is_saved", "ix_content_user_saved"),
    ("content", "is_new_upload", "ix_content_user_newupload"),
    ("users", "refresh_interval_minutes", None),
)


def _drop_removed_columns() -> None:
    """Drop _REMOVED_COLUMNS from an older database; a no-op once applied. Not a migration framework."""
    dropped: list[str] = []
    with engine.begin() as conn:
        for table, column, index in _REMOVED_COLUMNS:
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in present:
                continue
            # DROP COLUMN refuses while an index still references the column, so the index goes first.
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


# Must stay nullable with no default, so `create_all` and ALTER TABLE agree without a backfill.
_ADDED_CONTENT_COLUMNS = (
    ("artist_credit", "VARCHAR(300)"),
)


def _add_missing_columns() -> None:
    """Add _ADDED_CONTENT_COLUMNS to an older database: `create_all` adds tables but never columns."""
    with engine.begin() as conn:
        present = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(content)")}
        missing = [(c, t) for c, t in _ADDED_CONTENT_COLUMNS if c not in present]
        if not missing:
            return

        for column, ddl_type in missing:
            conn.exec_driver_sql(f"ALTER TABLE content ADD COLUMN {column} {ddl_type}")

    logger.info("Added missing content columns: %s", ", ".join(c for c, _ in missing))


def _rename_email_to_username() -> None:
    """Rename users.email to username in place, shortening to the local part where unique."""
    with engine.begin() as conn:
        present = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if "email" not in present or "username" in present:
            return

        conn.exec_driver_sql("ALTER TABLE users RENAME COLUMN email TO username")

        rows = list(conn.exec_driver_sql("SELECT id, username FROM users"))
        # Decided across the whole table first so the result doesn't depend on row order;
        # a local part two accounts want (or an existing value) goes to neither.
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

    # Must run before anything can start a download: only then is a ".part" file safe to delete.
    removed = sweep_startup_leftovers()
    if removed:
        logger.info("Removed %d abandoned .part file(s) from a previous run", removed)
    with SessionLocal() as db:
        reset = reset_interrupted_downloads(db)
    if reset:
        logger.info("Reset %d download(s) a previous run left unfinished", reset)

    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Spotea", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=resolve_secret_key(settings),
    session_cookie="spotea_session",
    same_site="lax",
    # Off by default: most installs are plain HTTP on a LAN.
    https_only=settings.session_https_only,
)

# Added after SessionMiddleware, so these run outside it — see middleware.install.
install_middleware(app)

class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles with "no-cache": without Cache-Control, browsers' heuristic freshness serves stale
    CSS/JS against new templates after an upgrade."""

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
app.include_router(offline_router.router)
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
    """Liveness that can fail: database reachable and scheduler loop alive. Cheap enough to poll."""
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
    # Served from the root: a service worker only controls paths at or below its own URL.
    # No require_login: installability checks may fetch this before there's a session.
    return FileResponse(
        "app/static/js/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# Avatars and thumbnails are content-addressed and never overwritten, so they can be cached forever.
_IMAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/avatars/{filename}", dependencies=[Depends(require_login)])
def get_avatar(filename: str) -> FileResponse:
    # Path traversal guard: filename arrives as attacker-controlled input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.avatars_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)


# YouTube Music serves art from yt3 and lh3 unpredictably (lh3 can't be rewritten to ggpht),
# and mood-playlist tracks use i.ytimg.com. Exact hostname match only, never a substring test.
_IMAGE_PROXY_ALLOWED_HOSTS = {
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "lh3.googleusercontent.com",
    "i.ytimg.com",
}
_IMAGE_PROXY_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}

# 1x1 transparent PNG served when an upstream fetch fails.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# Not cached, so a transient upstream failure doesn't freeze a blank image in the browser.
_IMAGE_PROXY_BLANK_HEADERS = {"Cache-Control": "no-store"}


@app.get("/image-proxy", dependencies=[Depends(require_login)])
def image_proxy(u: str) -> Response:
    """Stream a YouTube CDN image without storing it (hotlinking hits Chrome ORB and our own CSP).

    A failed fetch answers with a blank pixel, not an error, so the placeholder shows instead of a
    broken-image glyph.
    """
    host = urllib.parse.urlparse(u).hostname
    if host not in _IMAGE_PROXY_ALLOWED_HOSTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    fetched = fetch_image_bytes(u)
    if fetched is None:
        # Logged, or a broken proxy would look like channels that merely have no avatar.
        logger.warning("Image proxy could not fetch %s — serving a blank placeholder", u)
        return Response(
            content=_BLANK_PNG, media_type="image/png", headers=_IMAGE_PROXY_BLANK_HEADERS
        )

    body, content_type = fetched
    return Response(content=body, media_type=content_type, headers=_IMAGE_PROXY_CACHE_HEADERS)


@app.get("/thumbnails/{filename}", dependencies=[Depends(require_login)])
def get_thumbnail(filename: str) -> FileResponse:
    # Path traversal guard: filename arrives as attacker-controlled input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.thumbnails_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)
