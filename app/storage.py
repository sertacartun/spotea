"""Disk usage and cleanup.

yt-dlp ".part" files are only safe to delete at startup, before any download can be writing one."""

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import Artist, Content, PlaylistItem
from app.timeutil import utcnow

# Distinct from yt-dlp's ".part": an export temp file is live while the export downloads.
EXPORT_TEMP_SUFFIX = ".export.tmp"


@dataclass
class StoredItem:
    id: int
    title: str
    channel_title: str | None
    size_bytes: int
    # For offline copies in the browser, which can't fetch cover/duration later.
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
    """Duck-types StorageUsage's `total_bytes`/`count` for the shared summary template."""

    total_bytes: int
    count: int


def _size_on_disk(file_path: str | None) -> int:
    if not file_path:
        return 0
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return 0


def collect_usage(db: Session, user_id: int) -> StorageUsage:
    """Commits: rows with a NULL file_size_bytes are measured once and written back."""
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
    """SUM/COUNT only, without collect_usage's per-row work; never writes, so NULL sizes count as 0."""
    total_bytes, count = (
        db.query(func.coalesce(func.sum(Content.file_size_bytes), 0), func.count(Content.id))
        .filter(Content.user_id == user_id, Content.status == "ready")
        .one()
    )
    return UsageSummary(total_bytes=total_bytes, count=count)


def purge_content(db: Session, content: Content) -> None:
    """Delete a Content row and its audio file (per-user, so never shared). Deliberately doesn't
    commit: callers purge many rows and commit once."""
    if content.file_path:
        Path(content.file_path).unlink(missing_ok=True)
    # The ORM cascade runs from Playlist, not Content, so playlist_items must be deleted explicitly.
    db.query(PlaylistItem).filter(PlaylistItem.content_id == content.id).delete(
        synchronize_session=False
    )
    db.delete(content)


def clear_all(db: Session, user_id: int) -> int:
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
    """Call only at startup, before any download could be writing a .part file."""
    removed = 0
    # rglob: audio lives in per-user subdirectories.
    for part_file in settings.storage_dir.rglob("*.part"):
        part_file.unlink(missing_ok=True)
        removed += 1
    return removed


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
