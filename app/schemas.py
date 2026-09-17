from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.content_query import QUEUE_MAX_ITEMS
from app.images import is_music_video, track_artwork, track_cover

if TYPE_CHECKING:  # import cycle otherwise
    from app.models import Content

# SQLite doesn't enforce VARCHAR lengths, so these bounds are the only limit on request bodies.
_URL_MAX_LENGTH = 2048
_CONTENT_TITLE_MAX_LENGTH = 500  # Content.title: String(500)


class ArtistCreate(BaseModel):
    channel_url: str = Field(min_length=1, max_length=_URL_MAX_LENGTH)


class ArtistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: str
    name: str | None
    avatar_url: str | None
    added_at: datetime
    # Resolved server-side; tells the client whose profile to open.
    browse_id: str | None = None


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    artist_id: int
    channel_title: str | None
    # browse_id, else the Topic channel id (placeholder artists); None renders as plain text.
    artist_page_id: str | None = None
    video_id: str
    title: str
    thumbnail_url: str | None
    # MediaSession sizes; also the only cover the OS can fetch for an offline (blob:) track.
    artwork: list[dict[str, str]] = []
    duration_seconds: int | None
    published_at: datetime | None
    status: str
    added_at: datetime
    is_favorite: bool
    is_played: bool
    is_unavailable: bool = False
    # The player asks the server to swap in the song version when true.
    is_music_video: bool = False

    @classmethod
    def from_content(cls, content: "Content") -> "ContentOut":
        """Requires `content.artist` loaded. Not model_validate: several fields are derived, and
        thumbnail_url must use the track_cover fallback because the player draws from this payload."""
        return cls(
            id=content.id,
            artist_id=content.artist_id,
            channel_title=content.display_artist,
            artist_page_id=content.artist.browse_id or content.artist.channel_id,
            video_id=content.video_id,
            title=content.title,
            thumbnail_url=track_cover(content),
            artwork=track_artwork(content),
            duration_seconds=content.duration_seconds,
            published_at=content.published_at,
            status=content.status,
            added_at=content.added_at,
            is_favorite=content.is_favorite,
            is_played=content.last_played_at is not None,
            is_unavailable=content.is_unavailable,
            is_music_video=is_music_video(content),
        )


class QueueOut(BaseModel):
    ids: list[int]


class StatusOut(BaseModel):
    id: int
    status: str
    error_message: str | None = None
    progress_percent: int | None = None
    phase: str | None = None
    # The player treats this as "skip now" rather than "failed".
    is_unavailable: bool = False
    # Only set by POST /{id}/download, which may have swapped a music video for its song.
    content: ContentOut | None = None


class FavoriteOut(BaseModel):
    id: int
    is_favorite: bool


class OfflineTrackOut(BaseModel):
    id: int
    title: str
    channel_title: str | None
    thumbnail_url: str | None
    duration_seconds: int | None
    status: str
    is_unavailable: bool
    # Waiting in the download queue: still "not_downloaded", but not stuck.
    queued: bool


class OfflineTracksIn(BaseModel):
    """A list the server can't name (an album, a YouTube playlist): the device keeps its ids."""

    # "list:<kind>:<id>", the device's name for it; pins are stored under it.
    key: str = Field(pattern=r"^list:", max_length=200)
    ids: list[int] = Field(max_length=QUEUE_MAX_ITEMS)


class OfflineListOut(BaseModel):
    tracks: list[OfflineTrackOut]


class LyricLineOut(BaseModel):
    text: str
    # Integer ms so comparisons against currentTime are exact.
    start_ms: int
    end_ms: int


class LyricsOut(BaseModel):
    """`lines: None` means the track has no lyrics — the common case, not an error."""

    lines: list[LyricLineOut] | None
    source: str | None = None


class ChannelSearchResultOut(BaseModel):
    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


class VideoSearchResultOut(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None
    # Round-tripped via VideoAddCreate; the server can't recover it without re-asking YouTube Music.
    artist_credit: str | None = None
    # Unreliable on the yt-dlp fallback search, so consumers must handle None.
    channel_id: str | None = None


class PlaylistSearchResultOut(BaseModel):
    playlist_id: str
    title: str
    thumbnail_url: str | None
    channel_title: str | None


class MoodCategoryOut(BaseModel):
    title: str
    slug: str
    section: str


class RecommendationsOut(BaseModel):
    interests: list[str]
    interests_used: list[str]
    generated_at: datetime
    videos: list[VideoSearchResultOut]
    playlists: list[PlaylistSearchResultOut]
    # Shelves below don't depend on interests.
    charts: list[PlaylistSearchResultOut]
    chart_artists: list[ChannelSearchResultOut]
    moods: list[MoodCategoryOut]
    # Empty means exactly "nothing followed".
    similar_artists: list[ChannelSearchResultOut]


class VideoAddCreate(BaseModel):
    video_id: str
    title: str = Field(min_length=1, max_length=_CONTENT_TITLE_MAX_LENGTH)
    channel_id: str
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    channel_title: str | None = None
    artist_credit: str | None = Field(default=None, max_length=300)


class VideoAddResult(BaseModel):
    content_id: int


class VideoBatchItem(VideoAddCreate):
    """Same shape as a single add."""


class VideoBatchCreate(BaseModel):
    items: list[VideoBatchItem]


class VideoBatchResult(BaseModel):
    """In request order — that order is the play queue."""

    content_ids: list[int]


class ArtistAddResult(BaseModel):
    artist: ArtistOut


class SyncResult(BaseModel):
    # True when the library may have changed since the page rendered, so the client re-renders.
    checked: bool


class SettingsOut(BaseModel):
    audio_quality: str
    interests: list[str]


class SettingsUpdate(BaseModel):
    audio_quality: str | None = None
    # Always the complete list.
    interests: list[str] | None = None


# User-made playlists (models.Playlist), not YouTube playlists or Library's virtual lists.
class UserPlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class UserPlaylistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    track_count: int = 0
    # Only set by GET /playlists?content_id=…; None means not asked.
    contains: bool | None = None


class PlaylistTrackAdd(BaseModel):
    content_id: int
