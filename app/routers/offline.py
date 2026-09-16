"""Lists kept on a device: queueing their missing tracks, and pinning them so their files are
downloads (kept for good) rather than cache.

Which lists a device keeps lives on the device (IndexedDB); the server sees one list per call.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.content_query import query_content_by_ids, query_content_ids
from app.deps import get_current_user, get_db, require_login
from app.download_queue import DownloadQueue
from app.images import track_cover
from app.models import Content, OfflinePin, User
from app.page_context import playlist_filter, user_playlist_ids
from app.routers import content as content_router
from app.schemas import OfflineListOut, OfflineTrackOut, OfflineTracksIn

router = APIRouter(prefix="/offline", tags=["offline"], dependencies=[Depends(require_login)])

# Looked up per item, not bound here, so tests can swap the step out.
download_queue = DownloadQueue(lambda content_id: content_router.download_queued(content_id))


def _wanted(row: Content, retry: bool) -> bool:
    if row.is_unavailable:
        return False
    if row.status == "not_downloaded":
        return True
    # Only on an explicit tap: a poll re-queueing failures would retry them against YouTube forever.
    return retry and row.status == "error"


def _pin(db: Session, user_id: int, key: str, rows: list[Content]) -> None:
    """Replaces the list's pins with its current tracks, so a song taken off the list falls back to cache."""
    db.query(OfflinePin).filter(OfflinePin.user_id == user_id, OfflinePin.list_key == key).delete(
        synchronize_session=False
    )
    db.add_all(
        OfflinePin(user_id=user_id, list_key=key, content_id=content_id)
        for content_id in dict.fromkeys(row.id for row in rows)
    )
    db.commit()


def _queue_list(db: Session, user_id: int, key: str, ids: list[int] | None, *, retry: bool) -> OfflineListOut:
    if ids is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such playlist")
    rows = query_content_by_ids(db, user_id, ids)
    _pin(db, user_id, key, rows)
    download_queue.enqueue(row.id for row in rows if _wanted(row, retry))
    return OfflineListOut(
        tracks=[
            OfflineTrackOut(
                id=row.id,
                title=row.title,
                channel_title=row.display_artist,
                thumbnail_url=track_cover(row),
                duration_seconds=row.duration_seconds,
                status=row.status,
                is_unavailable=row.is_unavailable,
                queued=download_queue.is_queued(row.id),
            )
            for row in rows
        ]
    )


def _favorite_ids(db: Session, user_id: int) -> list[int]:
    return query_content_ids(db, user_id, filter=playlist_filter("favorites"))


@router.post("/favorites", response_model=OfflineListOut)
def favorites_download(
    retry: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> OfflineListOut:
    """Queues what isn't on the server yet and reports every track; `retry` also re-queues failures."""
    return _queue_list(db, user.id, "favorites", _favorite_ids(db, user.id), retry=retry)


@router.post("/playlists/{playlist_id}", response_model=OfflineListOut)
def playlist_download(
    playlist_id: int,
    retry: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OfflineListOut:
    return _queue_list(
        db, user.id, f"playlist:{playlist_id}", user_playlist_ids(db, user.id, playlist_id), retry=retry
    )


@router.post("/tracks", response_model=OfflineListOut)
def tracks_download(
    payload: OfflineTracksIn,
    retry: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OfflineListOut:
    """Another user's ids are dropped, and ids since removed simply drop out of the answer."""
    return _queue_list(db, user.id, payload.key, payload.ids, retry=retry)


@router.delete("/lists", status_code=status.HTTP_204_NO_CONTENT)
def unpin_list(
    key: str = Query(max_length=200), user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """A list no longer downloaded: its songs fall back to cache, unless another downloaded list holds them."""
    db.query(OfflinePin).filter(OfflinePin.user_id == user.id, OfflinePin.list_key == key).delete(
        synchronize_session=False
    )
    db.commit()
