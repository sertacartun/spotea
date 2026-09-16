"""Explore's result shapes, kept out of music.py so callers needn't import a YouTube Music client."""

from dataclasses import dataclass

# Per search, per kind: Explore's search fires on every keystroke.
SEARCH_RESULT_LIMIT = 8

# Some playlists run to thousands of entries, and the list renders in one go.
PLAYLIST_ITEM_LIMIT = 50


@dataclass
class ChannelSearchResult:
    """An artist card; the id is a YouTube Music browse id (the UC id for an artist with a channel).

    `subscriber_count` is None on search results — artist search doesn't carry one.
    """

    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


@dataclass
class VideoSearchResult:
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None
    channel_id: str | None = None
    # Joined credit ("Baby Keem, Kendrick Lamar"). Kept separate from `channel_title`: in one field,
    # the first track's joined credit would become the Artist row's name for the whole channel.
    artist_credit: str | None = None


@dataclass
class PlaylistSearchResult:
    playlist_id: str
    title: str
    thumbnail_url: str | None
    channel_title: str | None


@dataclass
class PlaylistDetail:
    playlist_id: str
    title: str | None
    video_count: int | None
    items: list[VideoSearchResult]
