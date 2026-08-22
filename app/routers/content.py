from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.content_query import query_content_ids
from app.database import SessionLocal
from app.deps import get_current_user, get_db, require_login
from app.downloader import DownloadError, VideoUnavailableError, download_audio
from app.formatting import safe_filename
from app.images import is_music_video, needs_thumbnail_caching
from app.models import Content, SwappedVideo, User
from app.page_context import playlist_filter
from app.progress import ProgressRegistry
from app.schemas import ContentOut, FavoriteOut, LyricsOut, QueueOut, StatusOut
from app.services.artist_follow import get_or_create_placeholder
from app.services.artist_sync import cache_thumbnail
from app.services.lyrics import lyrics_for
from app.timeutil import utcnow
from app.youtube.music import find_song_version
from app.youtube.urls import VIDEO_ID_RE

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_login)])

# In-memory only: fine for a single-process app, and progress ticks too
# frequently to justify a DB write on every hook call. Entries are dropped
# as soon as the download settles (see _run_download's finally), so the
# registry's expiry never actually comes into play here — it's the same
# type the backfill/import trackers use (see app/progress.py) rather than a
# fourth hand-rolled dict.
_download_progress: ProgressRegistry[int, tuple[str, int | None]] = ProgressRegistry()

# Keyed by extension rather than the configured AUDIO_FORMAT so files
# downloaded under a previous format setting still get a correct Content-Type.
AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


def _get_content_or_404(db: Session, content_id: int, user_id: int) -> Content:
    content = (
        db.query(Content).filter(Content.id == content_id, Content.user_id == user_id).first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


def _set_download_outcome(content_id: int, **fields) -> None:
    """Write a finished download's result on a session of this task's own.

    Deliberately NOT the request's `Depends(get_db)` session: since FastAPI
    0.106 a yield-dependency's exit code (get_db's `db.close()`) runs before
    the response is sent, i.e. before any BackgroundTask starts — so the
    session handed to a background task is already closed. SQLAlchemy
    happens to re-acquire a connection on next use, which is the only reason
    passing it here ever appeared to work; nothing guarantees that keeps
    being true. Opening a session inside the task is also what makes it safe
    to run for minutes on a worker thread, independent of the request that
    scheduled it."""
    with SessionLocal() as db:
        content = db.get(Content, content_id)
        if content is None:
            return
        for field, value in fields.items():
            setattr(content, field, value)
        db.commit()


def _run_download(content_id: int, video_id: str, quality: str, user_id: int) -> None:
    def on_progress(phase: str, percent: int | None) -> None:
        _download_progress.set(content_id, (phase, percent))

    try:
        file_path = download_audio(
            video_id, quality=quality, on_progress=on_progress, user_id=user_id
        )
    except VideoUnavailableError as exc:
        # Settled, not provisional — start_download won't attempt it again
        # and the player skips it without waiting. See Content.is_unavailable.
        _set_download_outcome(
            content_id, status="error", error_message=str(exc)[:1000], is_unavailable=True
        )
        return
    except DownloadError as exc:
        _set_download_outcome(content_id, status="error", error_message=str(exc)[:1000])
        return
    finally:
        _download_progress.discard(content_id)

    # Measured here, once, rather than on every render that wants a storage
    # total — see Content.file_size_bytes. The file is guaranteed to exist
    # at this point (download_audio raises otherwise), but a stat failure
    # still shouldn't lose the download itself, so it falls back to
    # "unmeasured" and lets collect_usage's lazy backfill retry later.
    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        size_bytes = None

    _set_download_outcome(
        content_id,
        status="ready",
        file_path=str(file_path),
        file_size_bytes=size_bytes,
        downloaded_at=utcnow(),
        # A row can only reach here after being playable, so whatever made it
        # unavailable before (a licence that has since landed in this
        # country, a re-upload) no longer holds.
        is_unavailable=False,
    )


# Both registered ahead of /{content_id} for the same reason
# /recently-played is (see its comment below) — three path segments can't
# collide with a one-segment route, but keeping every literal-prefixed route
# above the catch-all is what stops the next one from being subtly shadowed.
@router.get("/queue/playlist/{kind}", response_model=QueueOut)
def playlist_queue(
    kind: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueueOut:
    """Same, for one of the four pinned virtual playlists."""
    filter_value = playlist_filter(kind)
    if filter_value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playlist")
    return QueueOut(ids=query_content_ids(db, user.id, filter=filter_value))


@router.get("/{content_id}", response_model=ContentOut)
def get_content(
    content_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ContentOut:
    """Single-item fetch — used by the Home player overlay (see home/overlay.js's
    openPlayer) to populate itself for a track without a full page
    navigation. joinedload's needed here (unlike _get_content_or_404, whose
    other callers never touch .artist) since channel_title comes from it."""
    content = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.id == content_id, Content.user_id == user.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    # Caches for next time only, same as pages.py's _queue_thumbnail_caching
    # — this response still carries whatever thumbnail_url is on file now.
    if needs_thumbnail_caching(content.thumbnail_url):
        background_tasks.add_task(cache_thumbnail, content.video_id, content.thumbnail_url)

    return ContentOut.from_content(content)


def _credited_artist_id(db: Session, content: Content, song, user_id: int) -> int | None:
    """The artist a swapped-in song should hang off, or None to keep the one
    the row already has.

    A music video uploaded by a label arrives attributed to the *label* —
    "HYBE LABELS" owns the channel the chart entry came from, so that is what
    the row records, and the player's artist line links there rather than to
    KATSEYE. The song version names the real artist, and the swap is the
    moment we find out who that is.

    Only ever moves a row off a **placeholder**. An artist the user actually
    followed is their own decision about where this track belongs, and
    re-pointing it would take the track off that artist's Library page.
    """
    artist = content.artist
    if not song.channel_id or artist is None or artist.followed:
        return None
    if song.channel_id == artist.channel_id:
        return None
    return get_or_create_placeholder(db, song.channel_id, song.channel_title, user_id).id


@router.post("/{content_id}/song-version", response_model=ContentOut)
def swap_in_song_version(
    content_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ContentOut:
    """Turns a music-video row into the song it is a video of.

    Explore's playlists are video playlists almost end to end (see
    music.find_song_version for the measurements), and a video entry is the
    worse copy of the track in every way that shows: a 16:9 still where the
    rest of the app draws square album art, no lyrics, and a recording with
    an intro on it. The song version has all three.

    Updated **in place** rather than inserted alongside. The client is
    holding this row's id — it is in the queue, it is what the player is
    opening — so a second row would mean the id being played and the id in
    the queue disagreeing, and queue.js drops a queue the playing track
    isn't in. Rewriting the row keeps every id valid and fixes the cover
    everywhere it is already rendered, not just in the player.

    Answers with the row either way: no match, an unresolvable title, or a
    row that isn't a video at all are all "nothing to do here", not errors.
    The caller plays what it gets back.
    """
    content = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.id == content_id, Content.user_id == user.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    if not is_music_video(content) or content.status != "not_downloaded":
        # Already the song, or already downloaded — rewriting video_id under
        # a file that has been fetched would orphan it and leave the row
        # pointing at audio it no longer names.
        return ContentOut.from_content(content)

    song = find_song_version(
        content.title,
        content.artist.name if content.artist else None,
        content.artist.channel_id if content.artist else None,
    )
    if song is None:
        return ContentOut.from_content(content)

    # The unique constraint is on (user_id, video_id): this same song may
    # already be in the library from a search. Left alone in that case —
    # swapping would collide, and handing back the *other* row's id would
    # take the playing track out of the queue it came from.
    taken = (
        db.query(Content.id)
        .filter(Content.user_id == user.id, Content.video_id == song.video_id)
        .first()
    )
    if taken is not None:
        return ContentOut.from_content(content)

    # Resolved before anything is written: get_or_create_placeholder commits,
    # and calling it mid-mutation would land half of this swap.
    artist_id = _credited_artist_id(db, content, song, user.id)

    # Recorded before it's overwritten. The playlist this row came from still
    # lists the video's id, and POST /explore/tracks/batch looks rows up by
    # exactly that — so without this the next tap on the same row finds
    # nothing, creates a second row, and plays the music video's audio from
    # the start. See SwappedVideo for the measurements.
    db.add(SwappedVideo(user_id=user.id, video_id=content.video_id, content_id=content.id))

    if artist_id is not None:
        content.artist_id = artist_id
    content.video_id = song.video_id
    # The song's own title, not the uploader's. A chart entry arrives named
    # for the video file ("KATSEYE (캣츠아이) 'Hootie Frutti' Official MV"),
    # and once the row *is* the song, leaving that in place means every list
    # in the app still announces a music video the player is no longer
    # playing. Where the two already agree — which is most of a playlist —
    # this writes the same string back.
    content.title = song.title
    content.thumbnail_url = song.thumbnail_url
    if song.duration_seconds:
        content.duration_seconds = song.duration_seconds
    db.commit()
    db.refresh(content)
    return ContentOut.from_content(content)


# Registered ahead of the /{content_id}/... routes below — a literal segment
# placed after them would otherwise be swallowed by /{content_id} (Starlette
# matches path structure first and only fails int conversion once the
# request is already committed to that route), and no request would ever
# reach this one.
@router.delete("/recently-played")
def clear_recently_played(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, int]:
    # Deliberately leaves play_count alone — this clears the *history* shown
    # on the Recently Played shelf, not the play-frequency signal play_count
    # tracks (see models.py). Resetting both would make clearing your
    # history also erase what you actually listen to a lot.
    cleared = (
        db.query(Content)
        .filter(Content.user_id == user.id, Content.last_played_at.isnot(None))
        .update({"last_played_at": None}, synchronize_session=False)
    )
    db.commit()
    return {"cleared": cleared}


@router.post("/{content_id}/download", response_model=StatusOut)
def start_download(
    content_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusOut:
    content = _get_content_or_404(db, content_id, user.id)

    if content.status == "downloading":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already downloading")

    # Already on disk — say so instead of fetching it a second time. The
    # player never asks for a ready track (prepareAudio checks its own
    # dataset first), but the queue's one-track-ahead prefetch
    # (home/overlay.js) fires without knowing the next track's status, and
    # re-downloading everything it looks at would be the opposite of what
    # it's for. Still re-downloads when the row says ready but the file is
    # gone (storage cleared out from under us), which is the one case where
    # taking "ready" at face value would strand playback.
    if content.status == "ready" and content.file_path and Path(content.file_path).exists():
        return StatusOut(id=content.id, status=content.status, error_message=None)

    # YouTube has already told us, on every client, that it won't serve this
    # one (see Content.is_unavailable). Answering from the row costs nothing
    # and keeps the queue's prefetch — which fires for whatever is next
    # without knowing anything about it — from re-running the whole ladder
    # against YouTube on every pass over a track that can't work. DELETE
    # /content/{id} clears the flag, which is the way back if this ever
    # becomes wrong.
    if content.is_unavailable:
        return StatusOut(
            id=content.id,
            status=content.status,
            error_message=content.error_message,
            is_unavailable=True,
        )

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    background_tasks.add_task(
        _run_download, content.id, content.video_id, user.audio_quality, user.id
    )

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)


@router.get("/{content_id}/status", response_model=StatusOut)
def get_status(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> StatusOut:
    content = _get_content_or_404(db, content_id, user.id)
    phase, percent = _download_progress.get(content_id, (None, None))
    return StatusOut(
        id=content.id,
        status=content.status,
        error_message=content.error_message,
        progress_percent=percent,
        phase=phase,
        is_unavailable=content.is_unavailable,
    )


@router.post("/{content_id}/favorite", response_model=FavoriteOut)
def add_favorite(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> FavoriteOut:
    content = _get_content_or_404(db, content_id, user.id)
    content.is_favorite = True
    # Favoriting an Explore preview is a strong enough "keep this" signal on
    # its own to promote it out of preview status.
    content.is_preview = False
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.delete("/{content_id}/favorite", response_model=FavoriteOut)
def remove_favorite(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> FavoriteOut:
    content = _get_content_or_404(db, content_id, user.id)
    content.is_favorite = False
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.get("/{content_id}/lyrics", response_model=LyricsOut)
def track_lyrics(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> LyricsOut:
    """This track's timed lyrics, for the player panel's Lyrics tab.

    Only ever reached by opening that tab. Nothing calls this when a track
    starts playing, because a cache miss costs two live YouTube requests and
    most tracks turn out to have no lyrics — see services/lyrics.py.

    `lines: null` is a normal answer ("this track has none"), not an error.
    """
    content = _get_content_or_404(db, content_id, user.id)
    return LyricsOut(**lyrics_for(db, content.video_id))


@router.get("/{content_id}/stream")
def stream_content(
    content_id: int,
    download: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    content = _get_content_or_404(db, content_id, user.id)

    if content.status != "ready" or not content.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Content is not ready")

    file_path = Path(content.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing on disk")

    # Skipped for a plain file export (?download=1) — nobody's actually
    # listening to that.
    if not download:
        content.last_played_at = utcnow()
        db.commit()

    media_type = AUDIO_MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(
        file_path, media_type=media_type, filename=safe_filename(content.title) + file_path.suffix
    )


@router.delete("/{content_id}", response_model=StatusOut)
def delete_content(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> StatusOut:
    content = _get_content_or_404(db, content_id, user.id)

    if content.file_path:
        Path(content.file_path).unlink(missing_ok=True)

    content.status = "not_downloaded"
    content.file_path = None
    content.file_size_bytes = None
    content.error_message = None
    content.downloaded_at = None
    # Removing a download is the app's only "start over on this track"
    # action, so it doubles as the way to re-attempt one that was written off
    # as unavailable — YouTube licensing does change, and a flag with no way
    # back would make that permanent on our side even after it stopped being
    # true on theirs.
    content.is_unavailable = False
    db.commit()

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)
