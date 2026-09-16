"""Template context shared by full page renders and fragment re-renders, so both always agree."""

import json
from collections.abc import Iterable
from typing import NamedTuple

from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.content_query import (
    DEFAULT_PAGE_SIZE,
    count_content,
    followed_artists,
    query_content_by_ids,
    query_content_page,
)
from app.images import needs_thumbnail_caching
from app.interests import ONBOARDING_MIN_INTERESTS, interest_chips, parse_interests
from app.models import Artist, Content, Playlist, PlaylistItem, User
from app.services.artist_sync import cache_thumbnail, snapshot_releases
from app.services.initial_sync import syncing_artist_ids
from app.storage import backfill_file_sizes, storage_split
from app.timeutil import utcnow

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def queue_thumbnail_caching(background_tasks: BackgroundTasks, items: Iterable[Content]) -> None:
    """Queue thumbnail caching for what this response renders; benefits the next render, not this one."""
    seen: set[str] = set()
    for item in items:
        if not needs_thumbnail_caching(item.thumbnail_url):
            continue
        if item.video_id in seen:
            continue
        seen.add(item.video_id)
        background_tasks.add_task(cache_thumbnail, item.video_id, item.thumbnail_url)


def _new_releases(db: Session, user_id: int, limit: int | None = HOME_SHELF_LIMIT) -> list[dict]:
    """Followed artists' releases from `Artist.release_snapshot` — no network, this calendar year only.

    Year is the only date YouTube Music publishes (and it's the listing's year, so reissues count as new).
    Interleaved across artists so a prolific one can't fill every slot.
    """
    this_year = str(utcnow().year)
    artists = followed_artists(db, user_id).all()
    per_artist = [
        [
            {**entry, "artist_name": artist.name, "artist_id": artist.id}
            for entry in snapshot_releases(artist.release_snapshot)
            if entry.get("year") == this_year
        ]
        for artist in artists
    ]

    merged: list[dict] = []
    for position in range(max((len(items) for items in per_artist), default=0)):
        for items in per_artist:
            if position < len(items):
                merged.append(items[position])
    return merged if limit is None else merged[:limit]


def _shelf_query(db: Session, user_id: int):
    # is_preview: an Explore video not yet favorited shouldn't look like it's in the library.
    return (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.user_id == user_id, Content.is_preview.is_(False))
    )


def home_context(db: Session, user_id: int) -> dict:
    """Home's channel chips and shelves, each a separately bounded query."""
    recent_artists = followed_artists(db, user_id).limit(HOME_CHANNEL_LIMIT).all()
    interests = parse_interests(db.query(User.interests).filter(User.id == user_id).scalar())

    return {
        "home_recent_artists": recent_artists,
        # Derived rather than stored: a column would need a migration (create_all never adds columns).
        "show_onboarding": not recent_artists and not interests,
        "interest_chips": interest_chips(interests),
        "onboarding_min_interests": ONBOARDING_MIN_INTERESTS,
        "has_content": db.query(Content.id).filter(Content.user_id == user_id).first() is not None,
        "home_new_releases": _new_releases(db, user_id),
        # Not _shelf_query: a played Explore preview belongs here even though it's still is_preview.
        "home_recently_played": (
            db.query(Content)
            .options(joinedload(Content.artist))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .order_by(Content.last_played_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        "home_favorites": (
            _shelf_query(db, user_id)
            .filter(Content.is_favorite.is_(True))
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
    }


# No home_new_releases: those aren't Content rows, so there's no thumbnail of ours to cache.
HOME_SHELF_KEYS = ("home_recently_played", "home_favorites")


def home_shelf_items(context: dict) -> list[Content]:
    return [item for key in HOME_SHELF_KEYS for item in context[key]]


def _artist_release_count(artist: Artist) -> int:
    """Albums/singles in the artist's last release snapshot; 0 if unsynced or unparseable."""
    if not artist.release_snapshot:
        return 0
    try:
        return len(json.loads(artist.release_snapshot))
    except (TypeError, ValueError):
        return 0


def library_context(db: Session, user_id: int) -> dict:
    """Library's grid context; every count must match the filter of the page its tile opens."""
    artists = followed_artists(db, user_id).all()
    playlists = (
        db.query(Playlist)
        .filter(Playlist.user_id == user_id)
        .order_by(Playlist.created_at, Playlist.id)
        .all()
    )
    return {
        "artists": artists,
        "playlists": playlists,
        "playlist_track_counts": playlist_track_counts(db, user_id),
        "preparing_artist_ids": syncing_artist_ids(artist.id for artist in artists),
        "artist_release_counts": {artist.id: _artist_release_count(artist) for artist in artists},
        # Filter must match content_query._content_query (is_preview), or a tile overcounts its page.
        "artist_track_counts": dict(
            db.query(Content.artist_id, func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_preview.is_(False))
            .group_by(Content.artist_id)
            .all()
        ),
        "favorites_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_favorite.is_(True))
            .scalar()
        ),
        "new_uploads_count": len(_new_releases(db, user_id, limit=None)),
        "recently_played_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .scalar()
        ),
    }


def storage_summary_context(db: Session, user_id: int, *, backfill: bool = False) -> dict:
    """`backfill` measures rows with no stored size first (a write): the full page does, fragments don't."""
    if backfill:
        backfill_file_sizes(db, user_id)
    split = storage_split(db, user_id)
    return {"downloads": split.downloads, "cache": split.cache}


class PinnedPlaylist(NamedTuple):
    """One of Library's pinned virtual playlists; `filter` is what _content_query dispatches on."""

    filter: str
    title: str
    empty_title: str
    empty_help: str
    empty_cta: str


EMPTY_CTA_HREF = "/#explore"

PLAYLIST_KINDS: dict[str, PinnedPlaylist] = {
    "favorites": PinnedPlaylist(
        "__favorites__",
        "Favorites",
        "Songs you like live here",
        "Tap the heart on any song and it lands in this list.",
        "Find something to play",
    ),
    "new-uploads": PinnedPlaylist(
        "__new_uploads__",
        "New releases",
        "No new releases yet",
        "When an artist you follow puts something out, it shows up here.",
        "Follow more artists",
    ),
    "recently-played": PinnedPlaylist(
        "__played__",
        "Recently Played",
        "Nothing played yet",
        "Songs you play show up here so you can get back to them.",
        "Find something to play",
    ),
}


def playlist_track_counts(db: Session, user_id: int) -> dict[int, int]:
    rows = db.execute(
        select(PlaylistItem.playlist_id, func.count(PlaylistItem.id))
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(Playlist.user_id == user_id)
        .group_by(PlaylistItem.playlist_id)
    ).all()
    return {playlist_id: count for playlist_id, count in rows}


def user_playlist_ids(db: Session, user_id: int, playlist_id: int) -> list[int] | None:
    """Content ids of one hand-made list in order, or None if it isn't this user's (a 404, not empty)."""
    owned = (
        db.query(Playlist.id)
        .filter(Playlist.id == playlist_id, Playlist.user_id == user_id)
        .one_or_none()
    )
    if owned is None:
        return None
    rows = (
        db.query(PlaylistItem.content_id)
        .filter(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position, PlaylistItem.id)
        .all()
    )
    return [row[0] for row in rows]


def user_playlist_detail_context(
    db: Session, user_id: int, playlist_id: int, page: int
) -> dict | None:
    """One hand-made playlist in the shared track-list panel shape, or None if it isn't this user's."""
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.user_id == user_id)
        .one_or_none()
    )
    if playlist is None:
        return None

    ids = user_playlist_ids(db, user_id, playlist_id) or []
    total_pages = max(1, -(-len(ids) // DEFAULT_PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * DEFAULT_PAGE_SIZE
    items = query_content_by_ids(db, user_id, ids[start : start + DEFAULT_PAGE_SIZE])

    return {
        "kind": "user-playlist",
        "artist": None,
        "title": playlist.name,
        "user_playlist": playlist,
        "playlist_id": playlist.id,
        "empty_message": "Nothing in this playlist yet",
        "empty_help": "Open a song and use the + beside the heart to put it here.",
        "empty_cta": "Find something to play",
        "empty_cta_href": EMPTY_CTA_HREF,
        "video_count": len(ids),
        "content": items,
        "page": page,
        "total_pages": total_pages,
        "start_index": start + 1,
        "base_url": f"/#user-playlist/{playlist.id}",
    }


def queue_panel_context(db: Session, user_id: int, ids: list[int]) -> dict:
    """Rows for the player's Queue panel; the browser owns the order and the current-track pointer."""
    return {"queue_items": query_content_by_ids(db, user_id, ids)}


def playlist_filter(kind: str) -> str | None:
    """The content filter for a virtual playlist kind, or None.

    Read off PLAYLIST_KINDS so "Play all" and the detail panel can never select different rows.
    """
    config = PLAYLIST_KINDS.get(kind)
    return config.filter if config else None


def playlist_detail_context(db: Session, user_id: int, kind: str, page: int) -> dict | None:
    config = PLAYLIST_KINDS.get(kind)
    if config is None:
        return None

    # "New releases" is releases, not tracks: no filter, pagination or Play all.
    if kind == "new-uploads":
        releases = _new_releases(db, user_id, limit=None)
        return {
            "kind": kind,
            "artist": None,
            "title": config.title,
            "releases": releases,
            "count_label": f"{len(releases)} release{'' if len(releases) == 1 else 's'} this year",
            "empty_message": config.empty_title,
            "empty_help": config.empty_help,
            "empty_cta": config.empty_cta,
            "empty_cta_href": EMPTY_CTA_HREF,
        }

    video_count = count_content(db, user_id, filter=config.filter)
    items, page, total_pages = query_content_page(db, user_id, page=page, filter=config.filter)

    return {
        "kind": kind,
        "artist": None,
        "title": config.title,
        "empty_message": config.empty_title,
        "empty_help": config.empty_help,
        "empty_cta": config.empty_cta,
        "empty_cta_href": EMPTY_CTA_HREF,
        "video_count": video_count,
        "content": items,
        "page": page,
        "total_pages": total_pages,
        "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
        # A real navigable hash route (ctrl-click), not the /partials fetch URL.
        "base_url": f"/#{kind}",
    }
