from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.content_query import followed_artists
from app.deps import get_current_user, get_db, require_login
from app.models import Artist, Content, OfflinePin, User
from app.schemas import (
    ArtistAddResult,
    ArtistCreate,
    ArtistOut,
    RefreshResult,
)
from app.services.artist_follow import AlreadyFollowingError, NotAnArtistError, follow_artist_by_url
from app.services.artist_sync import refresh_feeds as sync_refresh_feeds
from app.services.initial_sync import (
    mark_syncing,
    run_initial_sync_task,
    sync_progress,
    syncing_artist_ids,
)
from app.storage import purge_content

router = APIRouter(prefix="/artists", tags=["artists"], dependencies=[Depends(require_login)])


@router.post("", response_model=ArtistAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(
    payload: ArtistCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ArtistAddResult:
    try:
        artist, new_count = follow_artist_by_url(
            db,
            payload.channel_url,
            user.id,
            # Answer as soon as the row exists; the catalogue snapshot runs in the background.
            sync=False,
        )
    except AlreadyFollowingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already following") from exc
    except NotAnArtistError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Marked here, not inside the task, so the card the client renders next can't beat it.
    mark_syncing(artist.id)
    background_tasks.add_task(run_initial_sync_task, artist.id)

    return ArtistAddResult(artist=ArtistOut.model_validate(artist), new_content_count=new_count)

@router.get("/syncing", response_model=list[int])
def list_backfilling_feeds(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[int]:
    """The artists whose first sync is still running, polled by Library's cards."""
    feed_ids = [artist_id for (artist_id,) in db.query(Artist.id).filter(Artist.user_id == user.id)]
    return sorted(syncing_artist_ids(feed_ids))


@router.delete("/{artist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(
    artist_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """Unfollow without destroying downloaded, played or favorited content.

    If anything is kept, the artist row is downgraded to followed=False instead of
    deleted, so that content keeps working and re-following picks the row back up.
    """
    artist = db.query(Artist).filter(Artist.id == artist_id, Artist.user_id == user.id).first()
    if not artist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    content_rows = db.query(Content).filter(Content.artist_id == artist_id).all()
    for content in content_rows:
        keep = (
            content.status == "ready"
            or content.last_played_at is not None
            or content.is_favorite
            # Queued for a downloaded list, not yet ready.
            or db.query(OfflinePin.id).filter(OfflinePin.content_id == content.id).first() is not None
        )
        if not keep:
            purge_content(db, content)

    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.artist_id == artist_id).scalar()
    if remaining == 0:
        db.delete(artist)
    else:
        artist.followed = False
    db.commit()

    sync_progress.discard(artist_id)


@router.post("/refresh", response_model=RefreshResult)
def refresh_feeds(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RefreshResult:
    artists = followed_artists(db, user.id).all()
    return RefreshResult(new_content_count=sync_refresh_feeds(db, artists))
