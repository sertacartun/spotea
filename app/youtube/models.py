"""The shapes Explore speaks in, independent of where they were read from.

They used to live in search.py alongside the yt-dlp code that built them.
Once YouTube Music became the only source (see music.py), that module was
nothing but these definitions and a pile of dead extraction — so the shapes
moved here and the module went away. Keeping them out of music.py is
deliberate: routers, schemas and the recommendation cache all speak these,
and none of them should have to import a YouTube Music client to do it.
"""

from dataclasses import dataclass

# Per search, per kind. Explore's search box fires while someone is typing,
# so this is what one keystroke's worth of results costs.
SEARCH_RESULT_LIMIT = 8

# Opening a playlist is a deliberate click, not a keystroke, so it can afford
# a deeper fetch than a search — but still bounded: some playlists run to
# thousands of entries, and this list is rendered in one go.
PLAYLIST_ITEM_LIMIT = 50


@dataclass
class ChannelSearchResult:
    """An artist, as a card. Still named for the channel it used to be: the
    id is a YouTube Music browse id, which for an artist with an official
    channel *is* that channel's UC id (see music._artist_result).

    `subscriber_count` is None on a search result — YouTube Music's artist
    search doesn't carry one, measured live. It arrives on a chart entry and
    on the artist's own page, so the field stays.
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
    # The artist this track hangs off — for a song, their auto-generated
    # "<Artist> - Topic" channel, which is what a preview row needs to attach
    # to (see routers/explore.py's add_video_batch). Arrives free in the same
    # response, which is why a whole remote list can become rows without one
    # extra network call.
    channel_id: str | None = None
    # Everyone credited on *this track*, joined ("Baby Keem, Kendrick Lamar"),
    # or None when a single artist is credited and `channel_title` already
    # says everything there is to say.
    #
    # Deliberately separate from `channel_title`, which is the one artist the
    # `channel_id` above belongs to. They were one field, and the joined form
    # is what a preview row's Artist got named — so the first track to create
    # that row named it for every track on the channel (see
    # music._artist_names).
    artist_credit: str | None = None


@dataclass
class PlaylistSearchResult:
    playlist_id: str
    title: str
    thumbnail_url: str | None
    # Whoever published the playlist ("YouTube Music" for the auto-generated
    # mixes, a real channel otherwise). No track count: a search result
    # doesn't carry one, and a field that's always None is worse than no
    # field — the count is only known once the playlist itself is opened.
    channel_title: str | None


@dataclass
class PlaylistDetail:
    playlist_id: str
    title: str | None
    video_count: int | None
    items: list[VideoSearchResult]
