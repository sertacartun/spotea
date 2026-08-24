"""Template context for the regions of index.html that can be re-rendered.

Each builder here backs two callers: the full page render (routers/pages.py)
and a fragment endpoint (routers/partials.py) that re-renders just that
region after something changes. Sharing the builder is the point — a shelf
that means one thing on first load and another on refresh is exactly the
class of bug this replaced.
"""

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
from app.storage import collect_usage, usage_summary
from app.timeutil import utcnow

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def queue_thumbnail_caching(background_tasks: BackgroundTasks, items: Iterable[Content]) -> None:
    """Caches thumbnails for whatever's actually being rendered in this
    response — a Home shelf, a Library list's current page, a channel page's
    current page — rather than eagerly sweeping ahead of actual browsing (a
    prior version of this did that at startup; on a large library it meant
    downloading and storing thumbnails for things nobody was looking at yet).
    This render still goes out with the original YouTube URL for anything
    not yet cached — the queued task (see artist_sync.cache_thumbnail) only
    ever benefits the *next* time this same content is rendered, anywhere.
    Deduped per call so a video appearing in more than one shelf here isn't
    queued twice."""
    seen: set[str] = set()
    for item in items:
        if not needs_thumbnail_caching(item.thumbnail_url):
            continue
        if item.video_id in seen:
            continue
        seen.add(item.video_id)
        background_tasks.add_task(cache_thumbnail, item.video_id, item.thumbnail_url)


def _new_releases(db: Session, user_id: int, limit: int | None = HOME_SHELF_LIMIT) -> list[dict]:
    """Home's "New releases" shelf: what the artists you follow have put out,
    read entirely off `Artist.release_snapshot`.

    This replaced a shelf of Content rows flagged `is_new_upload`, which only
    ever held releases that appeared *after* the follow and expired after
    fourteen days — so on a library where the follows are recent it showed
    almost nothing, and on a settled one it showed nothing at all. Measured
    on the live database before this was written: nine such rows existed,
    six of them belonging to an artist no longer followed, leaving three
    cards that would all have aged out within a fortnight.

    It cannot be fixed by sorting Content rows by date instead, because the
    app has no release dates for tracks: `Content.published_at` records when
    this app first saw a track, and every row for a followed artist on that
    same database read as one of the last three days. The only place a real
    ordering exists is the artist page, which lists releases newest first
    and reports a year.

    **No network.** An earlier design fetched each followed artist's
    releases live, and was rejected for exactly that — Home is rendered
    entirely from the database and putting a request in front of it is a
    different kind of page. Nothing is fetched here either: the sync already
    reads the artist page and now keeps the title, year, kind and cover it
    was throwing away (see services/artist_sync.snapshot_releases).

    **This calendar year only.** The year is the only date YouTube Music
    publishes — measured across both surfaces that could carry one, there is
    no month, no day, no timestamp and no ISO date anywhere — so "new" can
    mean nothing finer than this. Without the filter the pool is each
    artist's last ~20 releases going back a decade, and an artist who last
    put something out in 2016 would eventually surface it here.

    One caveat that comes with it and cannot be worked around: `year` is the
    year of *this listing*, not of the original recording. A 2026 reissue of
    a 1969 album reports 2026 (measured: Jimmy Cliff's, whose own description
    calls it a 1969 album). Nothing in the response distinguishes the two.

    Interleaved across artists, for the same reason Explore's shelves are
    (see services/recommendations._merge_from_followed): a prolific artist
    with thirty releases this year would otherwise fill every slot and the
    rest would never appear. No sort after that — every entry shares a year,
    so there is nothing left to sort by, and the round-robin order is the
    one that keeps artists mixed.

    `limit` is None for Library's panel, which shows all of them; Home's
    shelf takes twelve and links there for the rest.
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
    # is_preview excludes Explore videos not yet favorited — see
    # routers/explore.py's add_single_video and routers/content.py's
    # add_favorite. Listening to one shouldn't look like it's already in
    # the library.
    return (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.user_id == user_id, Content.is_preview.is_(False))
    )


def home_context(db: Session, user_id: int) -> dict:
    """Home's channel chips and its four shelves.

    Each shelf is its own bounded query. This used to be one `.all()` over
    every content row the user had ever had, sliced per shelf in Python,
    which got very slow once backfilling full channel histories pushed that
    past a few thousand rows.
    """
    # Newest-first already; the chip row is just the most recently followed
    # few (with 100+ channels followed, the full list made that row an
    # endless horizontal scroll).
    recent_artists = followed_artists(db, user_id).limit(HOME_CHANNEL_LIMIT).all()
    interests = parse_interests(db.query(User.interests).filter(User.id == user_id).scalar())

    return {
        "home_recent_artists": recent_artists,
        # First run: nothing followed and no interests listed, so every shelf
        # on this page and most of Explore has nothing to build from. Derived
        # rather than stored — a "has been onboarded" column would need a
        # migration (create_all adds tables to an existing database, never
        # columns), and this answers the same question from data that is
        # already there. It also means it comes back if someone empties their
        # library, which is the moment it is useful again.
        #
        # Read by index.html, not by _home_shelves.html: what it decides now
        # is whether the interests overlay starts open and locked, and that
        # overlay is part of the page rather than of the refreshable Home
        # fragment. Which is the point — a fragment refresh mid-onboarding
        # can't yank the thing the user is currently filling in.
        "show_onboarding": not recent_artists and not interests,
        # The chips for that overlay, which is the only picker there is —
        # Settings' "Manage interests" and the first run open the same one.
        "interest_chips": interest_chips(interests),
        # Handed to the client rather than hard-coded there, so the floor the
        # Continue button enforces and the reason for that floor stay in the
        # same place (see interests.ONBOARDING_MIN_INTERESTS).
        "onboarding_min_interests": ONBOARDING_MIN_INTERESTS,
        # Drives the "nothing here yet" branch — a cheap existence check
        # rather than counting anything.
        "has_content": db.query(Content.id).filter(Content.user_id == user_id).first() is not None,
        "home_new_releases": _new_releases(db, user_id),
        # Not built on _shelf_query: an Explore preview that's actually been
        # played earns a spot here even though it's still is_preview (never
        # favorited) — otherwise playing something from Explore and
        # coming back to Home would make it look like nothing happened. New
        # uploads/Favorites have no such case, since neither implies the user
        # ever listened.
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


# Deliberately without home_new_releases: those are releases to browse, not
# Content rows, so there is no thumbnail of ours to cache for them (their
# covers are proxied straight from YouTube Music, see _proxied_cover_url).
HOME_SHELF_KEYS = ("home_recently_played", "home_favorites")


def home_shelf_items(context: dict) -> list[Content]:
    """Every Content row home_context put on a shelf — what the caller hands
    to _queue_thumbnail_caching. Listed explicitly so adding a shelf is a
    deliberate edit here rather than something that silently starts (or stops)
    getting its thumbnails cached."""
    return [item for key in HOME_SHELF_KEYS for item in context[key]]


def _artist_release_count(artist: Artist) -> int:
    """How many albums/singles this artist has, per the last sync's
    snapshot (see services/artist_sync.py) — free, since every followed
    artist row already carries it, and unlike artist_track_counts below it
    doesn't collapse to 0 for the common case of "followed, nothing new
    released since". A card with nothing synced yet (still preparing, or a
    snapshot that failed to parse) reads as 0 rather than raising."""
    if not artist.release_snapshot:
        return 0
    try:
        return len(json.loads(artist.release_snapshot))
    except (TypeError, ValueError):
        return 0


def library_context(db: Session, user_id: int) -> dict:
    """Library's channel grid: per-channel counts plus the three pinned
    virtual-playlist tiles.

    Each count matches its page's own filter exactly (see content_query.py's
    query_content_page) — a tile saying "12 videos" that opens onto a list of
    9 is worse than no count at all.
    """
    artists = followed_artists(db, user_id).all()
    playlists = (
        db.query(Playlist)
        .filter(Playlist.user_id == user_id)
        .order_by(Playlist.created_at, Playlist.id)
        .all()
    )
    return {
        "artists": artists,
        # The hand-made lists, rendered as tiles beside the three pinned ones
        # (see _library_grid.html). Their counts come from one grouped query
        # for the same reason the artists' do.
        "playlists": playlists,
        "playlist_track_counts": playlist_track_counts(db, user_id),
        # Which cards say "Preparing…" — a channel whose one-time history scan
        # is still running (services/initial_sync.py). Read straight off the
        # in-memory registry, so this costs a dict lookup per card and no
        # query at all. It is the whole reason the onboarding wizard no
        # longer makes anyone wait for a backfill: the wait moved onto the
        # card of the channel it actually belongs to, where it can be ignored.
        "preparing_artist_ids": syncing_artist_ids(artist.id for artist in artists),
        # A followed artist's own release count — see _artist_release_count.
        # No query: every artist here is already loaded above.
        "artist_release_counts": {artist.id: _artist_release_count(artist) for artist in artists},
        # One grouped count covers every channel's card, rather than a
        # per-artist query each — count_content can't be reused directly here
        # for that reason (it's one artist_id at a time), but the filter has to
        # match it anyway: is_preview excludes Explore videos not yet
        # favorited, same as content_query._content_query, or a tile
        # can read a higher count than the channel page it opens onto lists
        # (measured live: 156 vs 154).
        #
        # Rarely the more interesting number for a followed artist's own
        # card: following only starts recording releases from here on (see
        # artist_sync.py), so this reads 0 for most artists most of the
        # time — that's correct, not a bug, and the template falls back to
        # artist_release_counts above for exactly that case.
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
        # Releases this year, matching exactly what the tile opens onto —
        # a count that disagrees with the page it leads to is worse than no
        # count (same rule as the docstring above).
        "new_uploads_count": len(_new_releases(db, user_id, limit=None)),
        "recently_played_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .scalar()
        ),
    }


def downloads_context(db: Session, user_id: int) -> dict:
    """What's on disk, item by item — the Downloads modal's own list, plus
    (at initial page load only, see pages.py's home()) the Settings summary
    line rendered from the same object. The fragment endpoints that refresh
    these afterwards (routers/partials.py) split apart: the modal's full list
    is fetched only when actually opened (home/settings.js's
    setupDownloadsOverlay), the summary line on every refreshFragments()
    sweep — see storage_summary_context below for that cheaper path.
    """
    return {"usage": collect_usage(db, user_id)}


def storage_summary_context(db: Session, user_id: int) -> dict:
    """Just the Settings summary line's two numbers, via
    storage.usage_summary rather than collect_usage — no per-row
    materialization, no joinedload. What refreshFragments() actually needs
    after a save/favorite/play; the full item list it used to also refetch
    was 86.5KB and, on the household's real library, 57% of the whole page's
    initial payload for a modal closed the vast majority of the time.
    """
    return {"usage": usage_summary(db, user_id)}


class PinnedPlaylist(NamedTuple):
    """One of Library's four pinned virtual playlists.

    A plain tuple until the empty state grew from a single sentence into a
    heading, an explanation of how the playlist fills up, and a way out of it
    — at five positional fields, which one was which stopped being obvious at
    the call site.

    `filter` is what _content_query (content_query.py) dispatches on. An
    earlier fourth element repeated that same filter as a lambda and lost its
    last reader when count_content replaced the hand-rolled count queries; it
    is deliberately not back.
    """

    filter: str
    title: str
    empty_title: str
    empty_help: str
    empty_cta: str


# Every empty state sends people to the same place, because Explore is the
# only place in the app where new music comes from.
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
    """How many tracks each of this user's playlists holds, in one query.

    Two callers: Library's grid, which renders a count on every tile, and
    GET /playlists, which returns the same number. One grouped count rather
    than one COUNT per playlist on every Library render — the same shape that
    made Content's per-artist counts a single grouped query.
    """
    rows = db.execute(
        select(PlaylistItem.playlist_id, func.count(PlaylistItem.id))
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(Playlist.user_id == user_id)
        .group_by(PlaylistItem.playlist_id)
    ).all()
    return {playlist_id: count for playlist_id, count in rows}


def user_playlist_ids(db: Session, user_id: int, playlist_id: int) -> list[int] | None:
    """Every content id in one hand-made list, in the order the user put them.

    None when the playlist isn't this user's — which the callers turn into a
    404 rather than an empty list, since "no such playlist" and "a playlist
    with nothing in it" are different answers and the empty state says so.

    The whole list rather than a page of it: the two callers are the detail
    panel (which pages this in Python — a hand-made list is small, and the
    alternative is a windowed join for a query that returns tens of rows) and
    "Play all", which wants all of them anyway.
    """
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
    """One hand-made playlist, through the same panel every other track list
    uses. None for a playlist that isn't this user's.

    The shape deliberately matches playlist_detail_context's above: same keys,
    same template, so a row here is identical to a Favorites row rather than a
    second list rendered by a second set of markup.
    """
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
        # What the panel needs to offer the two actions only this kind has:
        # removing one track from the list, and deleting the list itself.
        "user_playlist": playlist,
        "playlist_id": playlist.id,
        "empty_message": "Nothing in this playlist yet",
        "empty_help": "Open a song and use the + beside the heart to put it here.",
        "empty_cta": "Find something to play",
        "empty_cta_href": EMPTY_CTA_HREF,
        # len(ids), not len(items): the count line describes the playlist, not
        # the page of it on screen.
        "video_count": len(ids),
        "content": items,
        "page": page,
        "total_pages": total_pages,
        "start_index": start + 1,
        "base_url": f"/#user-playlist/{playlist.id}",
    }


def queue_panel_context(db: Session, user_id: int, ids: list[int]) -> dict:
    """What the player's "Queue" panel shows.

    The queue itself lives in the browser (static/js/home/queue.js owns the
    order, and shuffle makes it one the server never computed), so the ids
    arrive from the client and this only turns them into rows.

    All of them, in order, with no notion of which one is playing: that moves
    every time a track ends, and asking the server again each time is what
    used to rebuild the list under the user. The browser owns the pointer and
    marks the row itself.
    """
    return {"queue_items": query_content_by_ids(db, user_id, ids)}


def playlist_filter(kind: str) -> str | None:
    """The query_content_page/query_content_ids filter one virtual playlist
    (pinned or smart) means, or None for an unknown kind.

    Read off PLAYLIST_KINDS rather than spelled out again: the queue endpoint
    (routers/content.py) has to select exactly the rows the same playlist's
    detail panel renders, and a second copy of this mapping is how "Play all"
    would end up playing a different list than the one on screen.
    """
    config = PLAYLIST_KINDS.get(kind)
    return config.filter if config else None


def playlist_detail_context(db: Session, user_id: int, kind: str, page: int) -> dict | None:
    """One of Library's four pinned virtual playlists (see PLAYLIST_KINDS),
    rendered through the same track-list/pagination markup a channel page
    uses. Returns None for an unknown kind so the caller can 404."""
    config = PLAYLIST_KINDS.get(kind)
    if config is None:
        return None

    # One of these is not a track list. "New releases" holds albums and
    # singles read off Artist.release_snapshot — same cards Home's shelf of
    # that name shows, all of them rather than twelve, and rendered by
    # _releases_panel.html instead of the track-list body (see
    # _fragment_detail.html). It has no filter, no pagination and no Play
    # all; see that template for why.
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
        # A hash route, not the /partials/... fetch URL itself: this is what
        # _pagination.html's <a> actually points at, and home/detail.js's
        # click interception aside, it has to be a real navigable URL on its
        # own (ctrl-click, a JS-disabled fallback).
        "base_url": f"/#{kind}",
    }
