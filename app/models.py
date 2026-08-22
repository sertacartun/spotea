from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow

CONTENT_STATUSES = ("not_downloaded", "downloading", "ready", "error")


class User(Base):
    """One login, one library.

    Was two tables — an `Account` holding the credentials and one or more
    `User` profiles under it, Netflix-style. The household model is gone
    (one person, one library), so the credentials moved onto the row that
    already owned the artists and the content, and `accounts` went away.

    Email is always stored lowercased (normalized at the auth-router call
    sites), so a plain unique constraint is enough without a case-insensitive
    collation.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("audio_quality IN ('high', 'low')", name="ck_user_audio_quality"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    audio_quality: Mapped[str] = mapped_column(String(10), default="high")
    # Newline-separated free-text tags — genres, artists, moods — that
    # Explore's recommendations are built from. Parsed and written only
    # through app/interests.py, which owns the format (and the reason it
    # isn't a table of its own).
    interests: Mapped[str | None] = mapped_column(Text, default=None)
    # How often the background scheduler refreshes this library — see
    # scheduler.py.
    refresh_interval_minutes: Mapped[int] = mapped_column(default=30)
    # When the scheduler last refreshed it. None means never, which the
    # scheduler treats the same as "overdue" — so a fresh account's first
    # tick refreshes it immediately rather than waiting a full interval with
    # nothing to compare against.
    refreshed_at: Mapped[datetime | None] = mapped_column(default=None)

    artists: Mapped[list["Artist"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    content: Mapped[list["Content"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recommendation_cache: Mapped["RecommendationCache | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RecommendationCache(Base):
    """The last batch of interest-based Explore recommendations.

    Cached in the database rather than recomputed per request because
    building a batch means several live YouTube searches — seconds of
    latency, and request volume this app has good reason to keep low (see
    services/recommendations.py). One row per user: a batch is only ever
    read and replaced whole, never merged, so there's nothing to gain from
    storing the individual results as rows.

    `payload` is the JSON the API hands back verbatim; `interests_signature`
    is what the profile's interests hashed to when it was built (see
    interests.interests_signature), which is how an edit to the interest list
    invalidates it without anything having to explicitly delete this row.
    """

    __tablename__ = "recommendation_cache"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    interests_signature: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="recommendation_cache")


class Artist(Base):
    """A musician in the library.

    Was `Feed`, keyed by the RSS URL a sync used to read. Nothing reads RSS
    any more (see services/artist_sync.py), and what the library actually
    holds is artists — so the row is named for what it is and keyed by the
    ids that address one.

    `channel_id` is the artist's "<Artist> - Topic" channel: the container
    YouTube publishes their licensed audio to. It is the *key* rather than
    `browse_id` because it is what a track carries — a song grabbed from
    Explore hangs off the Topic channel, and it has to land on the same row
    as a deliberate follow of the same artist rather than making a second one.

    `browse_id` is how YouTube Music addresses their page, which for an
    artist with an official channel is that channel's id. It opens their
    profile and it is what the sync asks about. Null only on the placeholder
    rows below, which are created from a track and never resolved further.
    """

    __tablename__ = "artists"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_artist_user_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(500), default=None)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    # False only for placeholder rows auto-created to hold a single track
    # added via Explore (see services/artist_follow.py's get_or_create_placeholder)
    # — invisible in Library, skipped by the background refresh scheduler,
    # until the user actually follows the artist for real.
    followed: Mapped[bool] = mapped_column(default=True)
    browse_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # Every release browse id YouTube Music listed for this artist last time
    # we looked, as a JSON array. The whole change-detection mechanism: what
    # is on the page now and not in here is something they put out since (see
    # services/artist_sync.py). NULL means "never synced", which is what makes
    # a first sync record the catalogue without importing it.
    release_snapshot: Mapped[str | None] = mapped_column(Text, default=None)
    # YouTube Music's own bare count string ("1.91M"), refreshed on every
    # sync — see services/artist_sync.py. Comes back with every get_artist
    # call a sync already makes, so this costs nothing extra to keep, unlike
    # subscriber_count and description, which the same response carries but
    # nothing here persists.
    monthly_listeners: Mapped[str | None] = mapped_column(String(32), default=None)
    # YouTube Music's own "fans also like" list for this artist, as a JSON
    # array of ChannelSearchResult dicts — same free-data reasoning as
    # monthly_listeners above, and refreshed the same way on every sync.
    # What powers Explore's "Artists you may like" shelf (see
    # services/recommendations.py._similar_to_followed): merged across every
    # artist this user follows, rather than fetched fresh at request time.
    related_artists: Mapped[str | None] = mapped_column(Text, default=None)
    # The artist's own page-preview songs — YouTube Music's page shows five
    # before you have to open the full list (see youtube/music.py's
    # _artist_songs, which is what a sync's all_songs=False call reads) — as
    # a JSON array of VideoSearchResult dicts. Same free-data reasoning as
    # related_artists:
    # arrives on the same get_artist call a sync already makes. Powers
    # Explore's "Songs" shelf (see
    # services/recommendations.py._songs_from_followed) — merged across
    # every followed artist rather than searched from typed interests, which
    # went the same way genre-as-artist-search did (see similar_artists).
    top_tracks: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped["User"] = relationship(back_populates="artists")
    content: Mapped[list["Content"]] = relationship(back_populates="artist", cascade="all, delete-orphan")


class Content(Base):
    __tablename__ = "content"
    # Indexes below the first two were added after measuring the query plans on
    # a real 30k-row library: every one of them was answering a SCAN or a
    # USE TEMP B-TREE. Across the ten hottest queries this took 81.7ms of
    # SQLite time down to 3.8ms, and one operation from ~35 seconds to ~0.13
    # (unfollowing a 6,540-video channel, where purge_content does two
    # unindexed lookups per row). They cost nothing measurable in size: the
    # partial ones only cover the rows that actually match.
    #
    # The `sqlite_where` clauses are what makes them partial, and they are
    # dialect-specific — on any other backend these degrade to full indexes,
    # which is slower to write but still correct.
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_content_user_video_id"),
        Index("ix_content_user_status", "user_id", "status"),
        Index("ix_content_user_published_at", "user_id", "published_at"),
        # An artist's track list and its count: both filter user_id + artist_id
        # and order by published_at, which the (user_id, published_at) index
        # above could only answer by walking every row the user has.
        Index("ix_content_user_artist_published", "user_id", "artist_id", "published_at"),
        # Looked up by video_id alone — artist_sync.cache_thumbnail, which
        # runs per rendered item. The (user_id, video_id) unique constraint
        # can't serve it: video_id is its second column.
        Index("ix_content_video_id", "video_id"),
        # The pinned-playlist shelves and their counts. Partial, because
        # "played" and "favorite" are each a small slice of a library.
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
    # Size of file_path on disk, recorded once when the download finishes
    # (see routers/content.py's _run_download). Stored rather than stat'ed
    # on demand because storage.collect_usage runs on every Home render —
    # reading it from disk meant one stat syscall per downloaded track just
    # to render a total. Cleared alongside file_path whenever a download is
    # removed, and backfilled lazily for rows downloaded before this column
    # existed (see collect_usage).
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # True when the last download failed for a reason no retry can fix —
    # YouTube refuses this video id to every client there is, usually because
    # it's a "- Topic" art track licensed for other countries but not this
    # one (see downloader.is_permanent_failure). A separate flag rather than
    # a fifth `status` value because SQLite can't alter the CHECK constraint
    # above on an existing database, and because it *is* orthogonal: the row
    # is still an errored row, it just has a settled answer rather than a
    # provisional one. What it buys is that nothing re-attempts it — the
    # player skips it instantly instead of spending an extraction to be told
    # the same thing again, which is request volume that artists the very
    # rate-limiting the retry ladder exists for. Cleared by a successful
    # download and by DELETE /content/{id}, which is the manual "try this
    # again" path.
    is_unavailable: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    last_played_at: Mapped[datetime | None] = mapped_column(default=None)
    # True for a just-added Explore row that hasn't been favorited yet (see
    # routers/content.py's add_favorite, which clears this as a side effect)
    # — plays normally but stays out of Library and New Uploads until then.
    # Still shows on the Recently Played shelf once played (see
    # routers/pages.py's home_recently_played). No automatic cleanup — it
    # stays around indefinitely otherwise.
    #
    # Favoriting used to be one of two writers here; save-for-later was the
    # other, and it was removed along with its column. So the ways an Explore
    # row escapes preview status are now favoriting it or playing it, and
    # nothing else — which is also what storage.sweep_stale_previews checks
    # before deleting one.
    is_preview: Mapped[bool] = mapped_column(default=False)

    artist: Mapped["Artist"] = relationship(back_populates="content")
    user: Mapped["User"] = relationship(back_populates="content")


class SwappedVideo(Base):
    """A video id a Content row *used* to have, and the row it became.

    When a music-video entry is swapped for the song it is a video of (see
    routers/content.py's swap_in_song_version), the row's `video_id` changes.
    That is what makes the cover square and the lyrics arrive — and it also
    makes the row unfindable by the id the playlist it came from still shows.

    The consequence was a duplicate per tap. POST /explore/tracks/batch looks
    up "which of these do I already have" by video id; after a swap the answer
    for that playlist row is "none", so it created a second row, which then
    couldn't be swapped (the song is already taken by the first) and so played
    the music video's audio from the start. Measured on the live library: 205
    rows in an hour, one track stored three times, and the reported symptom —
    tapping the playing track again starts a different recording over.

    So the old id is kept here and the batch lookup consults it. Keyed by
    (user_id, video_id) because a Content row is per user, and the same
    playlist can be started by two of them.

    A new table rather than a column on `content`, for the reason spelled out
    in TrackLyrics below: `create_all` adds a missing table to an existing
    database but never a missing column.
    """

    __tablename__ = "swapped_videos"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_swapped_user_video_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # The id the row carried before the swap — what a playlist listing shows.
    video_id: Mapped[str] = mapped_column(String(20), index=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content.id", ondelete="CASCADE"))
    swapped_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrackLyrics(Base):
    """One track's timed lyrics, or the fact that it hasn't got any.

    Keyed by `video_id` rather than by a Content row: the same track can be
    several Content rows (a preview added from Explore and the same song
    picked up later by a sync, and one row per user besides), and the answer
    is a property of the recording, not of anyone's library.

    A new table rather than columns on `content` — which is not a stylistic
    choice. There is no migration framework here (see ARCHITECTURE.md), and
    `create_all` adds a missing *table* to an existing database but never a
    missing *column*. A table is therefore free to add and a column is not.

    Caching matters more than usual because a miss costs two live YouTube
    requests (see music.fetch_timed_lyrics) and, measured, about two thirds
    of tracks have no lyrics at all. So a negative answer is stored just as
    firmly as a positive one: `lines` NULL means "asked, there are none",
    which is different from having no row, meaning "never asked". Without
    that, the common case would re-ask YouTube every single time.

    Nothing expires these. Lyrics for a released recording don't change, and
    the app has no surface that would show a stale one.
    """

    __tablename__ = "track_lyrics"

    video_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    # JSON array of {text, start_ms, end_ms}. NULL = there are none.
    lines: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str | None] = mapped_column(String(200), default=None)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
