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
from app.page_context import playlist_filter, user_playlist_ids
from app.progress import ProgressRegistry
from app.schemas import ContentOut, FavoriteOut, LyricsOut, QueueOut, StatusOut
from app.services.artist_follow import get_or_create_placeholder
from app.services.artist_sync import cache_thumbnail
from app.services.lyrics import lyrics_for
from app.timeutil import utcnow
from app.youtube.music import find_song_version
from app.youtube.urls import VIDEO_ID_RE

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_login)])

# In-memory: progress ticks too often to justify a DB write per hook call.
_download_progress: ProgressRegistry[int, tuple[str, int | None]] = ProgressRegistry()

# Keyed by extension, not AUDIO_FORMAT, so files from a previous format setting still get the right type.
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
    """Write a finished download's result on the task's own session.

    The request's get_db session is already closed by the time a BackgroundTask runs.
    """
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
        # Settled: start_download won't retry it and the player skips it without waiting.
        _set_download_outcome(
            content_id, status="error", error_message=str(exc)[:1000], is_unavailable=True
        )
        return
    except DownloadError as exc:
        _set_download_outcome(content_id, status="error", error_message=str(exc)[:1000])
        return
    finally:
        _download_progress.discard(content_id)

        # A stat failure shouldn't lose the download; collect_usage backfills the size later.
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
        # It has been playable, so whatever made it unavailable no longer holds.
        is_unavailable=False,
    )


# Literal-prefixed routes stay above the /{content_id} catch-all so they aren't shadowed.
@router.get("/queue/playlist/{kind}", response_model=QueueOut)
def playlist_queue(
    kind: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueueOut:
    filter_value = playlist_filter(kind)
    if filter_value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playlist")
    return QueueOut(ids=query_content_ids(db, user.id, filter=filter_value))


@router.get("/queue/user-playlist/{playlist_id}", response_model=QueueOut)
def user_playlist_queue(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueueOut:
    """Separate from /queue/playlist: a hand-made list's order is stored, not a filter."""
    ids = user_playlist_ids(db, user.id, playlist_id)
    if ids is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such playlist")
    return QueueOut(ids=ids)


@router.get("/{content_id}", response_model=ContentOut)
def get_content(
    content_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ContentOut:
    """joinedload because channel_title comes from .artist."""
    content = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.id == content_id, Content.user_id == user.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    if needs_thumbnail_caching(content.thumbnail_url):
        background_tasks.add_task(cache_thumbnail, content.video_id, content.thumbnail_url)

    return ContentOut.from_content(content)


def _credited_artist_id(db: Session, content: Content, song, user_id: int) -> int | None:
    """The real artist a swapped-in song should hang off, or None to keep the current one.

    Only moves a row off a placeholder (e.g. a label channel), never off an artist the user followed.
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
    """Turn a music-video row into its song version, in place.

    Updated in place because the client holds this id in its queue; a new row would
    make queue.js drop the queue. No match is not an error: the row is returned as is.
    """
    content = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.id == content_id, Content.user_id == user.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    _apply_song_version(db, content, user.id)
    return ContentOut.from_content(content)


def _apply_song_version(db: Session, content: Content, user_id: int) -> None:
    if not is_music_video(content) or content.status != "not_downloaded":
        # Rewriting video_id under an already-downloaded file would orphan it.
        return

    song = find_song_version(
        content.title,
        content.artist.name if content.artist else None,
        content.artist.channel_id if content.artist else None,
    )
    if song is None:
        return

        # Unique on (user_id, video_id): if the song is already a row, leave this one alone.
    taken = (
        db.query(Content.id)
        .filter(Content.user_id == user_id, Content.video_id == song.video_id)
        .first()
    )
    if taken is not None:
        return

    # Resolved before any mutation: get_or_create_placeholder commits.
    artist_id = _credited_artist_id(db, content, song, user_id)

    # The source playlist still lists the video id, which /explore/tracks/batch looks up;
    # without this record the next tap would create a duplicate row.
    db.add(SwappedVideo(user_id=user_id, video_id=content.video_id, content_id=content.id))

    if artist_id is not None:
        content.artist_id = artist_id
    content.video_id = song.video_id
    # The song's own title: chart entries arrive named for the video file.
    content.title = song.title
    content.thumbnail_url = song.thumbnail_url
    # Written unconditionally, None included: a stale video credit is worse than falling back to the artist row.
    content.artist_credit = song.artist_credit
    if song.duration_seconds:
        content.duration_seconds = song.duration_seconds
    db.commit()
    db.refresh(content)


# Must be registered above /{content_id}/... routes, or /{content_id} swallows it.
@router.delete("/recently-played")
def clear_recently_played(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, int]:
    # Leaves play_count alone: clearing history must not erase the listen-frequency signal.
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

    # The queue prefetch fires without knowing the status; re-download only if the file is gone.
    if content.status == "ready" and content.file_path and Path(content.file_path).exists():
        return StatusOut(
            id=content.id,
            status=content.status,
            error_message=None,
            content=ContentOut.from_content(content),
        )

    # Answer from the row instead of re-running every client against YouTube;
    # DELETE /content/{id} clears the flag.
    if content.is_unavailable:
        return StatusOut(
            id=content.id,
            status=content.status,
            error_message=content.error_message,
            is_unavailable=True,
            content=ContentOut.from_content(content),
        )

    # Before the id check and scheduling, so the file fetched is the song, not the music video.
    _apply_song_version(db, content, user.id)

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    background_tasks.add_task(
        _run_download, content.id, content.video_id, user.audio_quality, user.id
    )
    # After the swap, so the thumbnail cached is the one the row ends up with.
    if needs_thumbnail_caching(content.thumbnail_url):
        background_tasks.add_task(cache_thumbnail, content.video_id, content.thumbnail_url)

    return StatusOut(
        id=content.id,
        status=content.status,
        error_message=content.error_message,
        content=ContentOut.from_content(content),
    )


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
    # Favoriting is a strong enough signal to promote an Explore preview.
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
    """Timed lyrics for the Lyrics tab; `lines: null` means the track has none.

    Only called when the tab opens: a cache miss costs two live YouTube requests.
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
    """Serve the audio file. Does not record a play: the prefetch requests this early.

    `?download=1` sets a filename, which makes Starlette send it as an attachment.
    """
    content = _get_content_or_404(db, content_id, user.id)

    if content.status != "ready" or not content.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Content is not ready")

    file_path = Path(content.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing on disk")

    media_type = AUDIO_MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=safe_filename(content.title) + file_path.suffix if download else None,
    )


@router.post("/{content_id}/played", status_code=status.HTTP_204_NO_CONTENT)
def record_played(
    content_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Record a play when the player actually starts the track.

    Not done in /stream, which fires for prefetches of tracks nobody may play.
    """
    content = _get_content_or_404(db, content_id, user.id)
    content.last_played_at = utcnow()
    db.commit()


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
    # Removing a download is the only way to re-attempt a track written off as unavailable.
    content.is_unavailable = False
    db.commit()

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)
