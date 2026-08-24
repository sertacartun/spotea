"""What's on disk, and what removes it once nothing needs it any more.

Three kinds of cleanup live here, triggered from different places for
different reasons:

  * A user action removing files it just made stale — clear_all,
    delete_content (routers/content.py), delete_feed (routers/artists.py) —
    each unlinks exactly the files its own action orphaned.
  * sweep_orphans, a directory-wide catch-all for files *no* row anywhere
    references any more — called after clear_all (so "Clear all" actually
    frees what it claims) and once per scheduler tick (see scheduler.py),
    so a channel unfollowed elsewhere, or an avatar search turned up and
    never followed, doesn't sit on disk forever.
  * sweep_stale_previews, a time-based retention sweep for Explore previews
    nobody ever did anything with — a different axis entirely (age, not
    "orphaned"), so it stays a separate function even though it's called
    from the same two places as sweep_orphans.

One kind of leftover deliberately isn't handled by any of the above: yt-dlp's
own ".part" files, from a download interrupted mid-write (typically
`docker compose up -d --build` killing the container). Unlike everything
above, there is no reliable "is this still needed" signal for a .part file —
a real download could be writing one at the exact moment any of these run.
The only moment that's provably safe is right after startup, before
anything has had a chance to start a download — see main.py's lifespan.
"""

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import Artist, Content, PlaylistItem
from app.timeutil import utcnow

# Suffix for the half-written archive routers/storage.py's export_all builds.
# Distinct from yt-dlp's own ".part" leftovers so sweep_orphans below can
# treat the two differently — an export in progress is live for as long as
# the download lasts (see STALE_EXPORT_AGE), whereas a stray ".part" never
# is (see the module docstring on why those are swept once at startup
# instead). Lives here rather than on the router so this module — the one
# that actually sweeps it — doesn't import from its own router to get it.
EXPORT_TEMP_SUFFIX = ".export.tmp"


@dataclass
class StoredItem:
    id: int
    title: str
    channel_title: str | None
    size_bytes: int
    # Carried for the copy the *browser* can keep (see static/js/offline.js),
    # not for anything this list renders: a track saved to the device stores
    # its own cover and duration alongside the audio, because a device with
    # no network can't go and ask for either later. The cover is fetched
    # through /image-proxy — same-origin, so its bytes are readable, which a
    # direct CDN fetch's opaque response would not be.
    thumbnail_url: str | None = None
    duration_seconds: int | None = None


@dataclass
class StorageUsage:
    items: list[StoredItem]
    total_bytes: int

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class UsageSummary:
    """Same two fields (`total_bytes`, `count`) a StorageUsage exposes, so
    the one Jinja template both render through (_fragment_storage_summary.html,
    also index.html's initial render) doesn't need to know which of the two
    it got. See usage_summary below for why this exists separately from
    StorageUsage."""

    total_bytes: int
    count: int


def _size_on_disk(file_path: str | None) -> int:
    """A file's size, or 0 if it's gone — a file removed out from under the
    app should read as 0 rather than inflate the total."""
    if not file_path:
        return 0
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return 0


def collect_usage(db: Session, user_id: int) -> StorageUsage:
    """Downloaded content plus the disk each file occupies.

    Sizes come from Content.file_size_bytes, recorded once when the download
    finished. They used to be stat'ed from disk on every call instead —
    which is one syscall per downloaded track on every single Home render
    (pages.py calls this to show one total), for a number that only ever
    changes when a download starts or is removed.

    Rows downloaded before that column existed have NULL and are measured
    here, once, then written back. That write is why this otherwise-read-only
    function commits.
    """
    rows = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.user_id == user_id, Content.status == "ready")
        .order_by(Content.downloaded_at.desc())
        .all()
    )

    items: list[StoredItem] = []
    needs_backfill = False
    for row in rows:
        if row.file_size_bytes is None:
            row.file_size_bytes = _size_on_disk(row.file_path)
            needs_backfill = True
        items.append(
            StoredItem(
                id=row.id,
                title=row.title,
                channel_title=row.display_artist,
                size_bytes=row.file_size_bytes,
                thumbnail_url=row.thumbnail_url,
                duration_seconds=row.duration_seconds,
            )
        )

    if needs_backfill:
        db.commit()

    return StorageUsage(items=items, total_bytes=sum(item.size_bytes for item in items))


def usage_summary(db: Session, user_id: int) -> UsageSummary:
    """Just the two numbers the Settings summary line needs — SUM and COUNT —
    without collect_usage's per-row materialization (a StoredItem, plus a
    joinedload(artist), built for every ready row). That row-by-row work was
    running on every single Home page render and, before this, on every
    save/favorite/play too (see fragments.js's refreshFragments) for a line
    that only ever changes when a download starts or is removed.

    A pre-existing row with a NULL file_size_bytes (predating the column —
    see migrations.py's _COLUMN_MIGRATIONS) is backfilled by collect_usage's
    own first call, not here: this never writes, so it stays correct as soon
    as anything has run collect_usage once — the very first Home page load
    already does — and undercounts by exactly the same NULL-until-backfilled
    amount collect_usage itself always read before that first call.
    """
    total_bytes, count = (
        db.query(func.coalesce(func.sum(Content.file_size_bytes), 0), func.count(Content.id))
        .filter(Content.user_id == user_id, Content.status == "ready")
        .one()
    )
    return UsageSummary(total_bytes=total_bytes, count=count)


def purge_content(db: Session, content: Content) -> None:
    """Delete a Content row and the files it was keeping alive.

    Both unlinks used to run a "is any other row still pointing at this?"
    query first, because two profiles under one account could hold separate
    rows for the same video and share the file on disk. That check was
    removed with profiles — on the reasoning that one library per login
    meant no second row to protect. Which was wrong the moment a second
    *account* existed: the audio file is named for the video id alone, so
    two accounts listening to the same track were still two rows over one
    file, and either one deleting it took the other's copy. Found on the
    live library, where one account's cleanup left another's row saying
    "ready" with nothing behind it.

    Audio is written per user now (see downloader.user_storage_dir), so
    there is genuinely nothing shared to protect any more — the unlink below
    can only ever remove this user's own copy. Thumbnails are still shared,
    and are deliberately left alone here: they are a few KB, they cost no
    YouTube request to rebuild, and sweep_orphans collects the ones nothing
    references at all.

    Deliberately does not commit: the unfollow path (routers/artists.py's
    delete_feed) purges many rows and commits once, and doing it per row
    would leave a half-purged artist behind if one of them failed.

    Note this is for rows that are genuinely going away — content.py's
    delete_content resets a row's download state in place and keeps the row,
    so it only unlinks the audio file.
    """
    if content.file_path:
        Path(content.file_path).unlink(missing_ok=True)
    # Every hand-made list holding this track. SQLite does not enforce the
    # foreign key unless PRAGMA foreign_keys is on, and the ORM cascade runs
    # from Playlist down, not from Content sideways — so without this the row
    # goes and its playlist_items stay, pointing at nothing. The list then
    # renders a gap and "Play all" queues an id that 404s.
    db.query(PlaylistItem).filter(PlaylistItem.content_id == content.id).delete(
        synchronize_session=False
    )
    db.delete(content)


def clear_all(db: Session, user_id: int) -> int:
    """Delete every downloaded file and reset its row. Returns rows cleared."""
    rows = db.query(Content).filter(Content.user_id == user_id, Content.status == "ready").all()

    for row in rows:
        if row.file_path:
            Path(row.file_path).unlink(missing_ok=True)
        row.status = "not_downloaded"
        row.file_path = None
        row.file_size_bytes = None
        row.error_message = None
        row.downloaded_at = None

    db.commit()
    sweep_orphans(db)

    return len(rows)


def sweep_startup_leftovers() -> int:
    """Deletes every yt-dlp ".part" file in the storage directory. Call this
    exactly once, at process startup, before anything else has had a chance
    to start a download — see the module docstring for why every other
    moment (a request, the scheduler tick) risks colliding with a real one
    still being written. Not DB-driven at all: unlike an orphaned audio file,
    there's nothing in the schema that tracks an in-progress download, so
    "the app just started" is the only signal that's ever safe. Returns the
    count removed.
    """
    removed = 0
    # rglob, not glob: audio is written into a directory per user (see
    # downloader.user_storage_dir), so a .part file is one level down. The
    # pattern still matches the flat layout older installs are on.
    for part_file in settings.storage_dir.rglob("*.part"):
        part_file.unlink(missing_ok=True)
        removed += 1
    return removed


# How long an EXPORT_TEMP_SUFFIX file (routers/storage.py's export_all) can
# sit unfinished before the sweep below treats it as abandoned rather than
# live. A real export is bounded by how long ZIP_STORED-ing the library
# takes — generous margin over even a very large one; the only way one
# survives past this is the request that was building it never got to
# finish (the container killed mid-export, same story as a stray .part).
STALE_EXPORT_AGE = timedelta(hours=1)


def sweep_orphans(db: Session) -> None:
    """Deletes on-disk files nothing points at any more: downloaded audio,
    cached thumbnails, cached avatars, and abandoned export archives.
    Directory-wide: it removes what no row anywhere still references.

    Called from clear_all (so "Clear all" actually frees what it claims) and
    once per scheduler tick (see scheduler.py), which
    is what catches orphans no single request causes on its own: a channel
    unfollowed without keeping anything, an avatar for a channel search
    turned up and nobody followed.

    Deliberately does NOT touch .part files — see the module-level docstring
    on why those are swept once at startup instead, not from here.
    """
    # Both sides resolved, not compared as the strings they happen to hold.
    # Content.file_path is written from settings.storage_dir as configured at
    # download time (see downloader.py), so the very same file is spelled
    # "data/storage/x.m4a" under one STORAGE_DIR and "/app/data/storage/x.m4a"
    # under another — and a plain string comparison then finds *nothing*
    # referenced and deletes the entire audio library on the next scheduler
    # tick. Not hypothetical: reproduced against this code, and it is what
    # emptied a real storage directory when the app was pointed at an existing
    # database with STORAGE_DIR spelled the other way (a throwaway test
    # instance, a different .env, anything).
    #
    # This still assumes a relative row is relative to the process's cwd,
    # which is the same assumption playback already makes — a run that gets
    # that wrong can't find its files at all, so a sweep is not where that
    # would first go wrong.
    referenced_audio = {
        Path(path).resolve()
        for (path,) in db.query(Content.file_path).filter(Content.file_path.isnot(None))
    }
    # rglob for the same reason as above. Files only — a per-user directory
    # is never itself a candidate, and an empty one costs nothing to leave.
    for leftover in settings.storage_dir.rglob(f"*.{settings.audio_format}"):
        if leftover.is_file() and leftover.resolve() not in referenced_audio:
            leftover.unlink(missing_ok=True)

    referenced_video_ids = {video_id for (video_id,) in db.query(Content.video_id)}
    for thumbnail in settings.thumbnails_dir.glob("*.jpg"):
        if thumbnail.stem not in referenced_video_ids:
            thumbnail.unlink(missing_ok=True)

    # Avatar filenames are "{channel_id}.jpg" (see images.download_avatar);
    # Artist.avatar_url stores the served path built from that same name, so
    # its basename is exactly what's on disk.
    referenced_avatars = {
        Path(url).name for (url,) in db.query(Artist.avatar_url).filter(Artist.avatar_url.isnot(None))
    }
    for avatar in settings.avatars_dir.glob("*.jpg"):
        if avatar.name not in referenced_avatars:
            avatar.unlink(missing_ok=True)

    cutoff = time.time() - STALE_EXPORT_AGE.total_seconds()
    for export_temp in settings.storage_dir.rglob(f"*{EXPORT_TEMP_SUFFIX}"):
        try:
            if export_temp.stat().st_mtime < cutoff:
                export_temp.unlink(missing_ok=True)
        except OSError:
            continue


# Both moved from a single AppSettings-style "no automatic cleanup" gap —
# see Content.is_preview's docstring, which was accurate before this existed.
# 7 days (not the 30 first proposed — locked decision): long enough to
# revisit something browsed a few days ago, short enough that idle previews
# from restless Explore browsing don't accumulate forever. Measured before
# this: 491 preview rows, and 217 followed=0 artists (75% of all artists) with
# nothing else ever cleaning either up.
PREVIEW_RETENTION = timedelta(days=7)


def sweep_stale_previews(db: Session) -> int:
    """Removes Explore previews nobody ever did anything with, once they're
    older than PREVIEW_RETENTION — same "did the user do anything with this"
    test routers/artists.py's delete_feed uses when deciding what an unfollow
    may remove (played, favorited, or actually downloaded keeps a row
    forever, however old). Also removes any placeholder artist (followed=False)
    a swept preview leaves with no content at all — the same cleanup
    delete_feed does on the unfollow path, needed here too for a placeholder
    nobody ever followed or unfollowed, just abandoned. Returns rows removed.
    """
    cutoff = utcnow() - PREVIEW_RETENTION
    stale = (
        db.query(Content)
        .filter(
            Content.is_preview.is_(True),
            Content.added_at < cutoff,
            Content.status != "ready",
            Content.last_played_at.is_(None),
            Content.is_favorite.is_(False),
        )
        .all()
    )
    if not stale:
        return 0

    touched_feed_ids = {row.artist_id for row in stale}
    for row in stale:
        purge_content(db, row)
    db.commit()

    empty_placeholder_ids = [
        artist_id
        for (artist_id,) in db.query(Artist.id)
        .filter(Artist.id.in_(touched_feed_ids), Artist.followed.is_(False))
        .filter(~Artist.content.any())
        .all()
    ]
    if empty_placeholder_ids:
        db.query(Artist).filter(Artist.id.in_(empty_placeholder_ids)).delete(synchronize_session=False)
        db.commit()

    return len(stale)
