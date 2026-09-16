"""Detecting new releases by followed artists via their YouTube Music page.

A set-diff of release ids against the stored snapshot is the whole change detection.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.images import download_avatar, download_thumbnail
from app.models import Artist, Content
from app.timeutil import utcnow
from app.youtube.models import ChannelSearchResult, VideoSearchResult
from app.youtube.music import ArtistRelease, fetch_artist, fetch_release

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


def snapshot_release_ids(snapshot: str | None) -> set[str]:
    """Just the browse ids, from both snapshot shapes.

    Legacy bare-id entries must count as seen, or the whole back catalogue would be re-imported.
    """
    if not snapshot:
        return set()
    try:
        entries = json.loads(snapshot)
    except (TypeError, ValueError):
        return set()
    ids = set()
    for entry in entries:
        if isinstance(entry, str):
            ids.add(entry)
        elif isinstance(entry, dict) and entry.get("browse_id"):
            ids.add(entry["browse_id"])
    return ids

# Unauthenticated requests: a larger burst risks 429s. DB writes never happen in the pool.
REFRESH_POOL_SIZE = 8


@dataclass
class ArtistFetchResult:
    """One artist's network round trip, ready to apply on the caller's session.

    `releases` is the full snapshot to store; `tracks` only the new releases' tracks (empty on a first sync).
    """

    ok: bool
    name: str | None = None
    avatar_url: str | None = None
    monthly_listeners: str | None = None
    related: list[ChannelSearchResult] | None = None
    # The artist's page-preview songs, unrelated to `tracks` (new-release tracks to insert).
    top_tracks: list[VideoSearchResult] | None = None
    releases: list[ArtistRelease] = field(default_factory=list)
    tracks: list[VideoSearchResult] = field(default_factory=list)


def fetch_artist_data(browse_id: str, snapshot: str | None, avatar_url: str | None) -> ArtistFetchResult:
    """The network half of a sync; touches no SQLAlchemy state, so safe in a thread pool.

    A first sync (`snapshot is None`) records what exists and imports nothing.
    """
    artist = fetch_artist(browse_id, all_songs=False)
    if artist is None:
        # A refresh is meant to survive one unreadable artist.
        logger.warning("Artist %s: no page to read", browse_id)
        return ArtistFetchResult(ok=False)

    releases = [*artist.albums, *artist.singles]

    fetched_avatar_url = None
    if not avatar_url and artist.avatar_url:
        # Once per artist ever, so steady-state refreshes add no call.
        fetched_avatar_url = download_avatar(browse_id, artist.avatar_url)

    if snapshot is None:
        return ArtistFetchResult(
            ok=True,
            name=artist.name,
            avatar_url=fetched_avatar_url,
            monthly_listeners=artist.monthly_listeners,
            related=artist.related,
            top_tracks=artist.tracks,
            releases=releases,
        )

    known = snapshot_release_ids(snapshot)
    kept = list(releases)
    tracks: list[VideoSearchResult] = []
    for release in releases:
        if release.browse_id in known:
            continue
        detail = fetch_release(release.browse_id)
        if detail is None:
            # Left out of the snapshot too, so the next refresh retries it.
            kept = [item for item in kept if item.browse_id != release.browse_id]
            continue
        tracks.extend(detail.tracks)

    return ArtistFetchResult(
        ok=True,
        name=artist.name,
        avatar_url=fetched_avatar_url,
        monthly_listeners=artist.monthly_listeners,
        related=artist.related,
        top_tracks=artist.tracks,
        releases=kept,
        tracks=tracks,
    )


def apply_artist_data(db: Session, artist: Artist, result: ArtistFetchResult) -> int:
    """The DB half of a sync; must run on the caller's session, never in the pool."""
    if not result.ok:
        return 0

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

    new_count = 0
    if result.tracks:
        # user_id-scoped: the (user_id, video_id) unique constraint spans artists.
        incoming_ids = [track.video_id for track in result.tracks]
        existing_ids = {
            video_id
            for (video_id,) in db.query(Content.video_id).filter(
                Content.user_id == artist.user_id, Content.video_id.in_(incoming_ids)
            )
        }
        seen = set(existing_ids)
        for track in result.tracks:
            if track.video_id in seen:
                continue
            seen.add(track.video_id)
            db.add(
                Content(
                    artist_id=artist.id,
                    user_id=artist.user_id,
                    video_id=track.video_id,
                    title=track.title,
                    thumbnail_url=track.thumbnail_url,
                    duration_seconds=track.duration_seconds,
                    # YouTube Music only reports a year, so first-seen time is the best date.
                    published_at=utcnow(),
                )
            )
            new_count += 1

    artist.release_snapshot = json.dumps([asdict(release) for release in result.releases])
    db.commit()
    return new_count


def refresh_feeds(db: Session, artists: list[Artist]) -> int:
    """Sync artists: fetches fanned out in a thread pool, applies sequential on the caller's session.

    Each apply is isolated so one artist failing doesn't abort the rest.
    """
    syncable = [artist for artist in artists if artist.browse_id]
    if not syncable:
        return 0

    with ThreadPoolExecutor(max_workers=min(len(syncable), REFRESH_POOL_SIZE)) as pool:
        results = list(
            pool.map(
                lambda f: fetch_artist_data(f.browse_id, f.release_snapshot, f.avatar_url),
                syncable,
            )
        )

    new_count = 0
    for artist, result in zip(syncable, results, strict=True):
        try:
            new_count += apply_artist_data(db, artist, result)
        except Exception:
            db.rollback()
            logger.exception("Failed to apply artist data for artist %s (%s)", artist.id, artist.name)
    return new_count


def cache_thumbnail(video_id: str, thumbnail_url: str) -> None:
    """Cache a thumbnail locally and rewrite every Content row sharing this video_id.

    Must never raise: FastAPI runs background tasks in sequence, and one that raises
    cancels every task queued behind it (e.g. the refresh-on-open).
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
