"""Hand-made playlists; the pinned lists are filters with no HTTP surface of their own."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.models import Content, OfflinePin, Playlist, PlaylistItem, User
from app.page_context import playlist_track_counts
from app.schemas import PlaylistTrackAdd, StatusOut, UserPlaylistCreate, UserPlaylistOut

router = APIRouter(prefix="/playlists", tags=["playlists"], dependencies=[Depends(require_login)])


def _get_playlist_or_404(db: Session, playlist_id: int, user_id: int) -> Playlist:
    playlist = (
        db.query(Playlist)
        .filter(Playlist.id == playlist_id, Playlist.user_id == user_id)
        .one_or_none()
    )
    if playlist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such playlist")
    return playlist


@router.get("", response_model=list[UserPlaylistOut])
def list_playlists(
    content_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[UserPlaylistOut]:
    """Every playlist, oldest first; `content_id` fills in `contains` for the add-to-playlist picker."""
    playlists = (
        db.query(Playlist)
        .filter(Playlist.user_id == user.id)
        .order_by(Playlist.created_at, Playlist.id)
        .all()
    )
    counts = playlist_track_counts(db, user.id)

    holding: set[int] = set()
    if content_id is not None:
        holding = {
            row[0]
            for row in db.execute(
                select(PlaylistItem.playlist_id)
                .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
                .where(Playlist.user_id == user.id, PlaylistItem.content_id == content_id)
            ).all()
        }

    return [
        UserPlaylistOut(
            id=playlist.id,
            name=playlist.name,
            created_at=playlist.created_at,
            track_count=counts.get(playlist.id, 0),
            contains=(playlist.id in holding) if content_id is not None else None,
        )
        for playlist in playlists
    ]


@router.post("", response_model=UserPlaylistOut, status_code=status.HTTP_201_CREATED)
def create_playlist(
    payload: UserPlaylistCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPlaylistOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Give the playlist a name"
        )

    # Pre-checked for a readable message; the unique constraint remains the race backstop.
    existing = (
        db.query(Playlist)
        .filter(Playlist.user_id == user.id, func.lower(Playlist.name) == name.lower())
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You already have a playlist called that"
        )

    playlist = Playlist(user_id=user.id, name=name)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return UserPlaylistOut(
        id=playlist.id, name=playlist.name, created_at=playlist.created_at, track_count=0
    )


@router.delete("/{playlist_id}", response_model=StatusOut)
def delete_playlist(
    playlist_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusOut:
    playlist = _get_playlist_or_404(db, playlist_id, user.id)
    # Cascade removes the items only; the Content rows belong to the library.
    db.delete(playlist)
    # A downloaded copy of this list stops holding its songs; they fall back to cache.
    db.query(OfflinePin).filter(
        OfflinePin.user_id == user.id, OfflinePin.list_key == f"playlist:{playlist_id}"
    ).delete(synchronize_session=False)
    db.commit()
    return StatusOut(id=playlist_id, status="deleted")


@router.post("/{playlist_id}/tracks", response_model=StatusOut)
def add_track(
    playlist_id: int,
    payload: PlaylistTrackAdd,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusOut:
    """Append a track; already present answers 200 "duplicate", not 409."""
    playlist = _get_playlist_or_404(db, playlist_id, user.id)

    # Scoped to this user, so one account cannot add another's track by id.
    content = (
        db.query(Content)
        .filter(Content.id == payload.content_id, Content.user_id == user.id)
        .one_or_none()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such track")

    already = (
        db.query(PlaylistItem)
        .filter(
            PlaylistItem.playlist_id == playlist.id,
            PlaylistItem.content_id == content.id,
        )
        .one_or_none()
    )
    if already is not None:
        return StatusOut(id=playlist.id, status="duplicate")

    # max+1, not a count: removals leave gaps, so a count could reuse an existing position.
    highest = (
        db.query(func.max(PlaylistItem.position))
        .filter(PlaylistItem.playlist_id == playlist.id)
        .scalar()
    )
    db.add(
        PlaylistItem(
            playlist_id=playlist.id,
            content_id=content.id,
            position=(highest or 0) + 1,
        )
    )
    db.commit()
    return StatusOut(id=playlist.id, status="added")


@router.delete("/{playlist_id}/tracks/{content_id}", response_model=StatusOut)
def remove_track(
    playlist_id: int,
    content_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusOut:
    playlist = _get_playlist_or_404(db, playlist_id, user.id)
    removed = (
        db.query(PlaylistItem)
        .filter(
            PlaylistItem.playlist_id == playlist.id,
            PlaylistItem.content_id == content_id,
        )
        .delete(synchronize_session=False)
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That track isn't in this playlist"
        )
    db.commit()
    # Gaps are left on purpose: only the order of positions is read.
    return StatusOut(id=playlist.id, status="removed")
