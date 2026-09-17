from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow

CONTENT_STATUSES = ("not_downloaded", "downloading", "ready", "error")


class User(Base):
    """One login, one library. Usernames are stored lowercased, so a plain unique constraint suffices."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("audio_quality IN ('high', 'low')", name="ck_user_audio_quality"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # SQLite ignores VARCHAR length; the real bound is routers/auth's MAX_USERNAME_LENGTH.
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    audio_quality: Mapped[str] = mapped_column(String(10), default="low")
    # Format owned by app/interests.py.
    interests: Mapped[str | None] = mapped_column(Text, default=None)
    # Last release check; due again once a refresh boundary passes (services/refresh.py).
    refreshed_at: Mapped[datetime | None] = mapped_column(default=None)

    artists: Mapped[list["Artist"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    content: Mapped[list["Content"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    playlists: Mapped[list["Playlist"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    recommendation_cache: Mapped["RecommendationCache | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RecommendationCache(Base):
    """Last Explore recommendation batch; invalidated when interests_signature no longer matches."""

    __tablename__ = "recommendation_cache"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    interests_signature: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="recommendation_cache")


class Artist(Base):
    """channel_id is the Topic channel a track carries, so a grabbed song and a follow share one row.
    browse_id addresses the artist page; null only on placeholder rows."""

    __tablename__ = "artists"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_artist_user_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(500), default=None)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    # False for placeholder rows holding a single Explore track; hidden and never refreshed.
    followed: Mapped[bool] = mapped_column(default=True)
    browse_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # JSON array of release ids seen last sync; new ids are new releases. NULL = never synced,
    # so the first sync records the catalogue without importing it.
    release_snapshot: Mapped[str | None] = mapped_column(Text, default=None)
    monthly_listeners: Mapped[str | None] = mapped_column(String(32), default=None)
    # JSON array of ChannelSearchResult dicts, refreshed on every sync.
    related_artists: Mapped[str | None] = mapped_column(Text, default=None)
    # JSON array of VideoSearchResult dicts, refreshed on every sync.
    top_tracks: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped["User"] = relationship(back_populates="artists")
    content: Mapped[list["Content"]] = relationship(back_populates="artist", cascade="all, delete-orphan")


class Content(Base):
    __tablename__ = "content"
    # sqlite_where makes some indexes partial; other backends get full indexes.
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_content_user_video_id"),
        Index("ix_content_user_status", "user_id", "status"),
        Index("ix_content_user_published_at", "user_id", "published_at"),
        Index("ix_content_user_artist_published", "user_id", "artist_id", "published_at"),
        # The unique constraint can't serve lookups by video_id alone (second column).
        Index("ix_content_video_id", "video_id"),
        Index(
            "ix_content_user_played",
            "user_id",
            "last_played_at",
            sqlite_where=text("last_played_at IS NOT NULL"),
        ),
        Index(
            "ix_content_user_favorite",
            "user_id",
            "published_at",
            sqlite_where=text("is_favorite = 1"),
        ),
        CheckConstraint(
            "status IN ('not_downloaded', 'downloading', 'ready', 'error')",
            name="ck_content_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), default=None)
    duration_seconds: Mapped[int | None] = mapped_column(default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(20), default="not_downloaded")
    file_path: Mapped[str | None] = mapped_column(String(500), default=None)
    # Stored so Settings' storage totals needn't stat every file on each render.
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # A flag, not a fifth status: SQLite can't alter the CHECK constraint on existing databases.
    # Set for permanent failures so nothing re-attempts; cleared by DELETE /content/{id}.
    is_unavailable: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    last_played_at: Mapped[datetime | None] = mapped_column(default=None)
    # Per-track credit; on the Artist row, the first track's credit would rename the artist.
    artist_credit: Mapped[str | None] = mapped_column(String(300), default=None)
    # Unfavorited, unplayed Explore row: hidden from Library, eligible for sweep_stale_previews.
    is_preview: Mapped[bool] = mapped_column(default=False)

    artist: Mapped["Artist"] = relationship(back_populates="content")

    @property
    def display_artist(self) -> str | None:
        """Requires `artist` to be loaded."""
        return self.artist_credit or (self.artist.name if self.artist else None)
    user: Mapped["User"] = relationship(back_populates="content")


class SwappedVideo(Base):
    """A Content row's pre-swap video id, so batch lookups by the playlist's id don't create duplicates.
    A table, not a column: `create_all` adds missing tables but never missing columns."""

    __tablename__ = "swapped_videos"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_swapped_user_video_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[str] = mapped_column(String(20), index=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content.id", ondelete="CASCADE"))
    swapped_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrackLyrics(Base):
    """Keyed by video_id (per recording). `lines` NULL = asked, none exist; no row = never asked.
    A table, not columns on content: `create_all` adds missing tables but never missing columns."""

    __tablename__ = "track_lyrics"

    video_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    # JSON array of {text, start_ms, end_ms}.
    lines: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str | None] = mapped_column(String(200), default=None)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class Playlist(Base):
    """A user-made playlist; Favorites/Recently Played are virtual filters with no rows."""

    __tablename__ = "playlists"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_playlist_user_name"),
        Index("ix_playlists_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="playlists")
    items: Mapped[list["PlaylistItem"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )


class PlaylistItem(Base):
    """Gaps in `position` are fine — only order matters, so appending is max+1."""

    __tablename__ = "playlist_items"
    __table_args__ = (
        UniqueConstraint("playlist_id", "content_id", name="uq_playlist_item"),
        Index("ix_playlist_items_playlist_position", "playlist_id", "position"),
        Index("ix_playlist_items_content", "content_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id"))
    content_id: Mapped[int] = mapped_column(ForeignKey("content.id"))
    position: Mapped[int] = mapped_column(default=0)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)

    playlist: Mapped["Playlist"] = relationship(back_populates="items")


class OfflinePin(Base):
    """A track a downloaded list holds. Its file is a download, kept for good; an unpinned
    file is cache, removed a week after its last play. `list_key` is the device's key for the
    list ("favorites", "playlist:12", "list:yt-release:MPREb_…")."""

    __tablename__ = "offline_pins"
    __table_args__ = (
        UniqueConstraint("user_id", "list_key", "content_id", name="uq_offline_pin"),
        Index("ix_offline_pins_content", "content_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    list_key: Mapped[str] = mapped_column(String(200))
    content_id: Mapped[int] = mapped_column(ForeignKey("content.id"))
