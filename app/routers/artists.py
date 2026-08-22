"""Following, unfollowing and refreshing channels.

The work itself lives in app/services (artist creation, the one-time history
backfill, bulk import) — this file is the HTTP surface over it: validation,
status codes, and deciding how each service call gets run (deferred to a
background task for a single add, inline for bulk import).
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.content_query import followed_artists
from app.deps import get_current_user, get_db, require_login
from app.models import Artist, Content, User
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
            # Answer as soon as the artist row exists. The catalogue snapshot
            # and the avatar behind it are what run_initial_sync does in the
            # background, and what Library's card reports while it happens.
            sync=False,
        )
    except AlreadyFollowingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already following") from exc
    except NotAnArtistError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Marked here rather than inside the task, so the card the client is
    # about to render cannot beat it to the question — see mark_syncing.
    mark_syncing(artist.id)
    background_tasks.add_task(run_initial_sync_task, artist.id)

    # Always 0: nothing has been fetched yet. Kept on the response for the
    # shape's sake; no caller of this route reads it.
    return ArtistAddResult(artist=ArtistOut.model_validate(artist), new_content_count=new_count)

@router.get("/syncing", response_model=list[int])
def list_backfilling_feeds(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[int]:
    """The artists still being filled in right now — their first
    RSS sync, or the one-time history scan behind it (see
    services/backfill.ACTIVE_PHASES).

    What Library's "Fetching uploads…" cards poll on, so they can turn
    back into a video count once the work behind them finishes (see
    home/library.js). Since POST /artists stopped syncing inline, a brand-new
    card is in this list from the moment it appears rather than showing a
    confident "0 videos" for the couple of seconds that took. One call for the whole grid rather than one
    backfill-status call per card, and it costs a dict lookup each — the
    registry is in memory.

    Declared above /{artist_id}/backfill-status only for readability; the two
    paths can't collide.
    """
    feed_ids = [artist_id for (artist_id,) in db.query(Artist.id).filter(Artist.user_id == user.id)]
    return sorted(syncing_artist_ids(feed_ids))


@router.delete("/{artist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(
    artist_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """Unfollowing isn't allowed to destroy what the user actually downloaded,
    played, favorited, or saved — only content nobody ever touched gets
    purged. Anything kept stays on the artist row, which is downgraded to
    followed=False (same state as an Explore placeholder — see
    get_or_create_placeholder) rather than deleted, so it drops out of
    Library/New releases/background refresh but keeps working everywhere else
    (Storage, Recently Played, Favorites/Saved, direct playback — none of
    those filter on Artist.followed). Re-following the same channel later picks
    this same row back up via services/artist_follow.py's follow_artist lookup
    instead of duplicating it."""
    artist = db.query(Artist).filter(Artist.id == artist_id, Artist.user_id == user.id).first()
    if not artist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    content_rows = db.query(Content).filter(Content.artist_id == artist_id).all()
    for content in content_rows:
        keep = (
            content.status == "ready"
            or content.last_played_at is not None
            or content.is_favorite
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
