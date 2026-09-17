"""Keeping followed artists' pages current: releases, listeners, related artists and top tracks.

One artist-page request per artist. Home's "New releases" shelf renders straight from the stored
release snapshot, so nothing here opens a release or imports its tracks.
"""

import json
import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.images import download_avatar, download_thumbnail
from app.models import Artist, Content
from app.youtube import music
from app.youtube.models import ChannelSearchResult, VideoSearchResult
from app.youtube.music import ArtistRelease, fetch_artist

logger = logging.getLogger(__name__)


def snapshot_releases(snapshot: str | None) -> list[dict]:
    """`Artist.release_snapshot` as release dicts; legacy bare-id entries are skipped."""
    if not snapshot:
        return []
    try:
        entries = json.loads(snapshot)
    except (TypeError, ValueError):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("browse_id")]


@dataclass
class ArtistFetchResult:
    """One artist's network round trip, ready to apply on the caller's session."""

    ok: bool
    name: str | None = None
    avatar_url: str | None = None
    monthly_listeners: str | None = None
    related: list[ChannelSearchResult] | None = None
    top_tracks: list[VideoSearchResult] | None = None
    releases: list[ArtistRelease] = field(default_factory=list)


def fetch_artist_data(browse_id: str, avatar_url: str | None) -> ArtistFetchResult:
    """The network half of a sync; touches no SQLAlchemy state, so safe in a thread pool."""
    artist = fetch_artist(browse_id, all_songs=False)
    if artist is None:
        # A sync is meant to survive one unreadable artist.
        logger.warning("Artist %s: no page to read", browse_id)
        return ArtistFetchResult(ok=False)

    fetched_avatar_url = None
    if not avatar_url and artist.avatar_url:
        # Once per artist ever, so steady-state syncs add no call.
        fetched_avatar_url = download_avatar(browse_id, artist.avatar_url)

    return ArtistFetchResult(
        ok=True,
        name=artist.name,
        avatar_url=fetched_avatar_url,
        monthly_listeners=artist.monthly_listeners,
        related=artist.related,
        top_tracks=artist.tracks,
        releases=[*artist.albums, *artist.singles],
    )


def apply_artist_data(db: Session, artist: Artist, result: ArtistFetchResult) -> None:
    """The DB half of a sync; must run on the caller's session, never in the pool."""
    if not result.ok:
        # The stored snapshot stays, so the shelf keeps what it had.
        return

    if result.name and not artist.name:
        artist.name = result.name
    if result.avatar_url:
        artist.avatar_url = result.avatar_url
    # Moving values: overwritten every sync, unlike name/avatar_url.
    if result.monthly_listeners:
        artist.monthly_listeners = result.monthly_listeners
    # `is not None`, not truthy: an empty list must clear a stale one.
    if result.related is not None:
        artist.related_artists = json.dumps([asdict(c) for c in result.related])
    if result.top_tracks is not None:
        artist.top_tracks = json.dumps([asdict(t) for t in result.top_tracks])

    artist.release_snapshot = json.dumps([asdict(release) for release in result.releases])
    db.commit()


def sync_artists(db: Session, artists: list[Artist]) -> None:
    """Fetches fanned out on the shared YouTube pool, applies sequential on the caller's session.

    Each apply is isolated so one artist failing doesn't abort the rest.
    """
    syncable = [artist for artist in artists if artist.browse_id]
    if not syncable:
        return

    results = list(music.pool.map(lambda a: fetch_artist_data(a.browse_id, a.avatar_url), syncable))

    for artist, result in zip(syncable, results, strict=True):
        try:
            apply_artist_data(db, artist, result)
        except Exception:
            db.rollback()
            logger.exception("Failed to apply artist data for artist %s (%s)", artist.id, artist.name)


def cache_thumbnail(video_id: str, thumbnail_url: str) -> None:
    """Cache a thumbnail locally and rewrite every Content row sharing this video_id.

    Must never raise: FastAPI runs background tasks in sequence, and one that raises
    cancels every task queued behind it.
    """
    try:
        local_url = download_thumbnail(video_id, thumbnail_url)
        if local_url and local_url != thumbnail_url:
            with SessionLocal() as db:
                db.query(Content).filter(
                    Content.video_id == video_id,
                    ~Content.thumbnail_url.like("/thumbnails/%"),
                ).update({"thumbnail_url": local_url}, synchronize_session=False)
                db.commit()
    except Exception:
        logger.exception("Could not cache the thumbnail for %s", video_id)
