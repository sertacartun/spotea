"""Detail-panel context for remote YouTube content, rendered through the same panel as library lists.

Kept out of page_context.py because these builders make slow network calls.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.images import cached_avatar_or_hotlink
from app.models import Artist
from app.routes import detail_path
from app.timeutil import utcnow
from app.youtube.music import (
    ARTIST_PREVIEW_SONGS,
    MoodCategory,
    fetch_artist,
    fetch_mood_categories,
    fetch_mood_playlists,
    fetch_playlist,
    fetch_release,
)
from app.youtube.urls import CHANNEL_PAGE_URL_TEMPLATE


def _base_context(kind: str, remote_id: str, title: str, items: list) -> dict:
    return {
        "kind": kind,
        "remote": True,
        "artist": None,
        "title": title,
        "content": items,
        "empty_message": "Nothing playable here.",
        "back_label": "Explore",
        # One page always: the fetch is capped and YouTube can't cheaply serve a later page.
        "page": 1,
        "total_pages": 1,
        "start_index": 1,
        "base_url": detail_path(kind, remote_id),
    }


def remote_playlist_context(playlist_id: str) -> dict | None:
    """A YouTube playlist's tracks, or None when it couldn't be read, so the caller can 404."""
    playlist = fetch_playlist(playlist_id)
    if not playlist.items:
        return None

    total = playlist.video_count or len(playlist.items)
    context = _base_context(
        "yt-playlist", playlist_id, playlist.title or "Playlist", playlist.items
    )
    context.update(
        {
            "video_count": total,
            # Says so when the fetch was capped, rather than implying these are all.
            "count_label": (
                f"First {len(playlist.items)} of {total} tracks"
                if total > len(playlist.items)
                else f"{len(playlist.items)} track{'' if len(playlist.items) == 1 else 's'}"
            ),
            "hero_image": playlist.items[0].thumbnail_url,
        }
    )
    return context


def _followed_artist_id(db: Session, user_id: int, channel_id: str) -> int | None:
    followed = (
        db.query(Artist)
        .filter(
            Artist.user_id == user_id,
            Artist.channel_id == channel_id,
            Artist.followed.is_(True),
        )
        .first()
    )
    return followed.id if followed else None


def _artist_or_channel(db: Session, user_id: int, browse_id: str):
    """The artist behind an id plus their hero/follow context, or None if nothing playable.

    Follow targets the Topic channel (releases only), falling back to the official channel, then browse id.
    """
    artist = fetch_artist(browse_id)
    if artist is None or not artist.tracks:
        return None

    follow_channel_id = artist.topic_channel_id or artist.channel_id or artist.browse_id
    return artist, {
        "hero_image": cached_avatar_or_hotlink(follow_channel_id, artist.avatar_url),
        "hero_is_avatar": True,
        "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=follow_channel_id),
        "followed_artist_id": _followed_artist_id(db, user_id, follow_channel_id),
        # The profile's own browse id, not the requested one: they differ after a VEVO redirect,
        # and the VEVO id would record the artist against a songless page.
        "browse_id": artist.browse_id,
    }


def remote_artist_context(
    db: Session, user_id: int, browse_id: str
) -> dict | None:
    """An artist's profile shelves; the songs are a preview of remote_artist_songs_context."""
    resolved = _artist_or_channel(db, user_id, browse_id)
    if resolved is None:
        return None
    artist, hero = resolved

    context = {
        "kind": "yt-artist",
        "remote": True,
        "artist": None,
        "title": artist.name,
        "back_label": "Explore",
        "description": artist.description,
        "count_label": (
            f"{artist.monthly_listeners} monthly listeners"
            if artist.monthly_listeners
            else f"{artist.track_count} tracks"
        ),
        "songs": artist.tracks[:ARTIST_PREVIEW_SONGS],
        "songs_total": artist.track_count if artist.track_count > ARTIST_PREVIEW_SONGS else 0,
        "songs_url": detail_path("yt-artist-songs", browse_id),
        "albums": artist.albums,
        "singles": artist.singles,
        "related": artist.related,
        # The year is the only release date YouTube Music reports.
        "current_year": str(utcnow().year),
    }
    context.update(hero)
    return context


def remote_artist_songs_context(
    db: Session, user_id: int, browse_id: str
) -> dict | None:
    """The artist's full track list, the profile's "See all", with the same hero and Follow."""
    resolved = _artist_or_channel(db, user_id, browse_id)
    if resolved is None:
        return None
    artist, hero = resolved

    shown = len(artist.tracks)
    count_label = (
        f"First {shown} of {artist.track_count} tracks"
        if artist.track_count > shown
        else f"{shown} track{'' if shown == 1 else 's'}"
    )

    context = _base_context("yt-artist-songs", browse_id, artist.name, artist.tracks)
    context.update({"video_count": shown, "count_label": count_label, **hero})
    # The button pops history, which leads back to the profile.
    context["back_label"] = artist.name
    return context


def remote_release_context(browse_id: str) -> dict | None:
    release = fetch_release(browse_id)
    if release is None:
        return None

    subtitle = " · ".join(
        part for part in (release.kind, release.year, release.artist_names) if part
    )
    context = _base_context("yt-release", browse_id, release.title, release.tracks)
    context.update(
        {
            # Entered from some artist profile, not necessarily this release's artist;
            # a wrong name is worse than a generic one.
            "back_label": "Back",
            "video_count": len(release.tracks),
            "count_label": (
                f"{subtitle} · {len(release.tracks)} track"
                f"{'' if len(release.tracks) == 1 else 's'}"
            ),
            "hero_image": release.cover_url,
        }
    )
    return context


# The mood menu rarely changes; remembering it keeps opening a mood at one request, not two.
MOOD_CATEGORIES_TTL = timedelta(hours=12)

_mood_categories: tuple[datetime, list[MoodCategory]] | None = None


def _mood_by_slug(slug: str) -> MoodCategory | None:
    """The category a /moods/{slug} URL names. A miss in the remembered menu re-fetches it, in case it changed."""
    global _mood_categories
    if _mood_categories is not None and utcnow() - _mood_categories[0] < MOOD_CATEGORIES_TTL:
        found = next((category for category in _mood_categories[1] if category.slug == slug), None)
        if found is not None:
            return found

    categories = fetch_mood_categories()
    if categories:
        _mood_categories = (utcnow(), categories)
    return next((category for category in categories if category.slug == slug), None)


def remote_mood_context(slug: str) -> dict | None:
    """A mood's playlists, rendered by _mood_panel.html since there is no single track list."""
    category = _mood_by_slug(slug)
    if category is None:
        return None

    playlists = fetch_mood_playlists(category.params)
    if not playlists:
        return None

    return {
        "kind": "yt-mood",
        "remote": True,
        "artist": None,
        "title": category.title,
        "back_label": "Explore",
        "playlists": playlists,
    }
