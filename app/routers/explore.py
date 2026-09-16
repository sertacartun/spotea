"""Explore: search YouTube Music and make results playable without following anything."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.models import Artist, Content, SwappedVideo, User
from app.schemas import (
    ChannelSearchResultOut,
    VideoAddCreate,
    VideoAddResult,
    VideoBatchCreate,
    VideoBatchResult,
    VideoSearchResultOut,
)
from app.services.artist_follow import get_or_create_placeholder
from app.storage import purge_content
from app.timeutil import utcnow
from app.youtube.music import search_artists, search_songs
from app.youtube.urls import CHANNEL_ID_RE

router = APIRouter(prefix="/explore", tags=["explore"], dependencies=[Depends(require_login)])


@router.get("/artists", response_model=list[ChannelSearchResultOut])
def search_feeds(q: str) -> list[ChannelSearchResultOut]:
    """Artists to follow, from the music catalogue rather than youtube.com channels."""
    query = q.strip()
    if not query:
        return []

    return [ChannelSearchResultOut(**result.__dict__) for result in search_artists(query)]


@router.get("/songs", response_model=list[VideoSearchResultOut])
def search_video_feeds(q: str) -> list[VideoSearchResultOut]:
    """Songs from YouTube Music; no youtube.com fallback, since this app only holds music."""
    query = q.strip()
    if not query:
        return []

    return [VideoSearchResultOut(**result.__dict__) for result in search_songs(query)]


@router.post("/tracks", response_model=VideoAddResult, status_code=status.HTTP_201_CREATED)
def add_single_video(
    payload: VideoAddCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoAddResult:
    """Explore's "listen": add one track as a preview without following its channel.

    An existing row for this video is not a conflict; its id is returned.
    """
    existing_content = (
        db.query(Content)
        .filter(Content.user_id == user.id, Content.video_id == payload.video_id)
        .first()
    )
    if existing_content is None:
        # Playing a music video swaps the row to the song; this result still names the video.
        existing_content = (
            db.query(Content)
            .join(SwappedVideo, SwappedVideo.content_id == Content.id)
            .filter(
                SwappedVideo.user_id == user.id,
                SwappedVideo.video_id == payload.video_id,
                Content.user_id == user.id,
            )
            .first()
        )
    if existing_content:
        return VideoAddResult(content_id=existing_content.id)

    artist = get_or_create_placeholder(db, payload.channel_id, payload.channel_title, user.id)

    content = Content(
        artist_id=artist.id,
        user_id=user.id,
        video_id=payload.video_id,
        title=payload.title,
        thumbnail_url=payload.thumbnail_url,
        duration_seconds=payload.duration_seconds,
        artist_credit=payload.artist_credit,
        # NULL sorts last in ORDER BY ... DESC on every Home shelf; "just added" is the intended date.
        published_at=utcnow(),
        is_preview=True,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    return VideoAddResult(content_id=content.id)


def _preview_content(artist_id: int, user_id: int, item) -> Content:
    return Content(
        artist_id=artist_id,
        user_id=user_id,
        video_id=item.video_id,
        title=item.title,
        thumbnail_url=item.thumbnail_url,
        duration_seconds=item.duration_seconds,
        artist_credit=item.artist_credit,
        published_at=utcnow(),
        is_preview=True,
    )


@router.post("/tracks/batch", response_model=VideoBatchResult, status_code=status.HTTP_201_CREATED)
def add_video_batch(
    payload: VideoBatchCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoBatchResult:
    """Turn a whole remote playlist into playable rows, in order, with no network calls.

    Retried on IntegrityError: a double tap sends two concurrent calls that both insert the
    same rows. The losing retry re-reads and just reports the winner's ids.
    """
    items = [item for item in payload.items if CHANNEL_ID_RE.match(item.channel_id)]
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No playable videos given")

    for attempt in range(BATCH_INSERT_ATTEMPTS):
        try:
            return _insert_batch(db, user.id, items)
        except IntegrityError as err:
            # Rolling back is what lets the re-read see the other request's rows.
            db.rollback()
            if attempt == BATCH_INSERT_ATTEMPTS - 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This list is already being started — try again",
                ) from err
    raise AssertionError("unreachable")


# Two concurrent calls need one retry; past a third, "try again" is the honest answer.
BATCH_INSERT_ATTEMPTS = 3



def _insert_batch(db: Session, user_id: int, items: list) -> VideoBatchResult:
    """One attempt; the retry must re-run the reads too, not just the writes."""
    wanted_video_ids = [item.video_id for item in items]
    existing_content = {
        content.video_id: content
        for content in db.query(Content).filter(
            Content.user_id == user_id,
            Content.video_id.in_(wanted_video_ids),
        )
    }
    # Rows swapped from music video to song no longer match this id (see SwappedVideo);
    # without this every one would get a duplicate.
    swapped = (
        db.query(SwappedVideo.video_id, Content)
        .join(Content, Content.id == SwappedVideo.content_id)
        .filter(
            SwappedVideo.user_id == user_id,
            SwappedVideo.video_id.in_(wanted_video_ids),
            Content.user_id == user_id,
        )
    )
    for original_video_id, content in swapped:
        existing_content.setdefault(original_video_id, content)

    wanted_channel_ids = {item.channel_id for item in items}
    artists_by_channel = {
        artist.channel_id: artist
        for artist in db.query(Artist).filter(
            Artist.user_id == user_id, Artist.channel_id.in_(wanted_channel_ids)
        )
    }

    for item in items:
        if item.channel_id not in artists_by_channel:
            # Same placeholder contract as get_or_create_placeholder, inline so the batch is one flush.
            artist = Artist(
                user_id=user_id,
                channel_id=item.channel_id,
                name=item.channel_title,
                followed=False,
            )
            db.add(artist)
            artists_by_channel[item.channel_id] = artist
    db.flush()

    created: dict[str, Content] = {}
    for item in items:
        if item.video_id in existing_content or item.video_id in created:
            continue
        artist = artists_by_channel[item.channel_id]
        content = _preview_content(artist.id, user_id, item)
        db.add(content)
        created[item.video_id] = content

    db.commit()

    resolved = {**{k: v.id for k, v in existing_content.items()}, **{k: v.id for k, v in created.items()}}
    return VideoBatchResult(content_ids=[resolved[item.video_id] for item in items])


@router.delete("/tracks/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_single_video(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """Remove an Explore-added track outright; only for content on a followed=False artist."""
    content = (
        db.query(Content)
        .join(Artist)
        .filter(Content.id == content_id, Content.user_id == user.id, Artist.followed.is_(False))
        .first()
    )
    if not content:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    artist_id = content.artist_id
    purge_content(db, content)
    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.artist_id == artist_id).scalar()
    if remaining == 0:
        db.query(Artist).filter(Artist.id == artist_id).delete()
        db.commit()
