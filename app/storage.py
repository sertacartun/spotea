"""Disk usage and cleanup.

yt-dlp ".part" files are only safe to delete at startup, before any download can be writing one."""

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Artist, Content, OfflinePin, PlaylistItem
from app.timeutil import utcnow

# Distinct from yt-dlp's ".part": an export temp file is live while the export downloads.
EXPORT_TEMP_SUFFIX = ".export.tmp"


@dataclass
class UsageSummary:

    total_bytes: int
    count: int


def _size_on_disk(file_path: str | None) -> int:
    if not file_path:
        return 0
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return 0


def backfill_file_sizes(db: Session, user_id: int) -> None:
    """Measures ready rows with no stored size (downloaded before the column existed, or a
    failed stat) once and writes it back, so storage_split can stay a plain SUM."""
    rows = (
        db.query(Content)
        .filter(
            Content.user_id == user_id,
            Content.status == "ready",
            Content.file_size_bytes.is_(None),
        )
        .all()
    )
    for row in rows:
        row.file_size_bytes = _size_on_disk(row.file_path)
    if rows:
        db.commit()


def purge_content(db: Session, content: Content) -> None:
    """Delete a Content row and its audio file (per-user, so never shared). Deliberately doesn't
    commit: callers purge many rows and commit once."""
    if content.file_path:
        Path(content.file_path).unlink(missing_ok=True)
    # The ORM cascade runs from Playlist, not Content, so playlist_items must be deleted explicitly.
    db.query(PlaylistItem).filter(PlaylistItem.content_id == content.id).delete(
        synchronize_session=False
    )
    db.query(OfflinePin).filter(OfflinePin.content_id == content.id).delete(synchronize_session=False)
    db.delete(content)


def is_pinned():
    return select(OfflinePin.id).where(OfflinePin.content_id == Content.id).exists()


@dataclass
class StorageSplit:
    """Settings' two storage rows: downloads (pinned by a downloaded list) and cache (the rest)."""

    downloads: UsageSummary
    cache: UsageSummary


def storage_split(db: Session, user_id: int) -> StorageSplit:
    """Never writes, so NULL sizes count as 0 until backfill_file_sizes has run."""
    pinned = is_pinned()
    total_bytes = func.coalesce(func.sum(Content.file_size_bytes), 0)
    rows = (
        db.query(pinned.label("pinned"), total_bytes, func.count(Content.id))
        .filter(Content.user_id == user_id, Content.status == "ready")
        .group_by(pinned)
        .all()
    )
    by_kind = {bool(is_pinned): UsageSummary(total_bytes=size, count=count) for is_pinned, size, count in rows}
    empty = UsageSummary(total_bytes=0, count=0)
    return StorageSplit(downloads=by_kind.get(True, empty), cache=by_kind.get(False, empty))


def _forget_file(row: Content) -> None:
    """The row stays in the library; only its file goes, so the next play downloads it again."""
    if row.file_path:
        Path(row.file_path).unlink(missing_ok=True)
    row.status = "not_downloaded"
    row.file_path = None
    # Reset too: a stale size would make the next backfill_file_sizes skip the row.
    row.file_size_bytes = None
    row.error_message = None
    row.downloaded_at = None


def clear_cache(db: Session, user_id: int) -> int:
    """Every file no downloaded list holds, whatever its age."""
    rows = (
        db.query(Content)
        .filter(Content.user_id == user_id, Content.status == "ready", ~is_pinned())
        .all()
    )
    for row in rows:
        _forget_file(row)
    db.commit()
    sweep_orphans(db)
    return len(rows)


CACHE_RETENTION = timedelta(days=7)


def sweep_cache(db: Session) -> int:
    """Cached files a week past their last play, or past their download if never played (a queue prefetch)."""
    cutoff = utcnow() - CACHE_RETENTION
    rows = (
        db.query(Content)
        .filter(
            Content.status == "ready",
            ~is_pinned(),
            func.coalesce(Content.last_played_at, Content.downloaded_at, Content.added_at) < cutoff,
        )
        .all()
    )
    for row in rows:
        _forget_file(row)
    db.commit()
    return len(rows)


def sweep_startup_leftovers() -> int:
    """Call only at startup, before any download could be writing a .part file."""
    removed = 0
    # rglob: audio lives in per-user subdirectories.
    for part_file in settings.storage_dir.rglob("*.part"):
        part_file.unlink(missing_ok=True)
        removed += 1
    return removed


def reset_interrupted_downloads(db: Session) -> int:
    """Call only at startup. A restart mid-download leaves the row "downloading" with nothing
    running it, and every path that would download it again skips a row in that state."""
    reset = (
        db.query(Content)
        .filter(Content.status == "downloading")
        .update({"status": "not_downloaded", "error_message": None}, synchronize_session=False)
    )
    db.commit()
    return reset


STALE_EXPORT_AGE = timedelta(hours=1)


def sweep_orphans(db: Session) -> None:
    """Deletes audio, thumbnails, avatars and stale export archives no row references."""
    # Compare resolved paths: the same file spelled relative vs. absolute would otherwise look
    # unreferenced, and the whole audio library gets deleted.
    referenced_audio = {
        Path(path).resolve()
        for (path,) in db.query(Content.file_path).filter(Content.file_path.isnot(None))
    }
    for leftover in settings.storage_dir.rglob(f"*.{settings.audio_format}"):
        if leftover.is_file() and leftover.resolve() not in referenced_audio:
            leftover.unlink(missing_ok=True)

    referenced_video_ids = {video_id for (video_id,) in db.query(Content.video_id)}
    for thumbnail in settings.thumbnails_dir.glob("*.jpg"):
        if thumbnail.stem not in referenced_video_ids:
            thumbnail.unlink(missing_ok=True)

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


PREVIEW_RETENTION = timedelta(days=7)


def sweep_stale_previews(db: Session) -> int:
    """Removes old untouched Explore previews and any placeholder artists left empty."""
    cutoff = utcnow() - PREVIEW_RETENTION
    stale = (
        db.query(Content)
        .filter(
            Content.is_preview.is_(True),
            Content.added_at < cutoff,
            Content.status != "ready",
            Content.last_played_at.is_(None),
            Content.is_favorite.is_(False),
            # Still queued for a downloaded list.
            ~is_pinned(),
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
