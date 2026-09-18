"""Re-render one region of index.html from the same context functions, for the client to swap in.

Each response is one or more <template data-target="…"> blocks.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.models import User
from app.page_context import (
    about_context,
    home_context,
    library_context,
    playlist_detail_context,
    queue_panel_context,
    queue_thumbnail_caching,
    storage_summary_context,
    user_playlist_detail_context,
)
from app.services.remote_detail import (
    remote_artist_context,
    remote_artist_songs_context,
    remote_mood_context,
    remote_playlist_context,
    remote_release_context,
)
from app.templating import templates
from app.youtube.urls import CHANNEL_ID_RE, MOOD_SLUG_MAX_LENGTH, MOOD_SLUG_RE, PLAYLIST_ID_RE, RELEASE_ID_RE

router = APIRouter(prefix="/partials", tags=["partials"], dependencies=[Depends(require_login)])


@router.get("/home", response_class=HTMLResponse)
def home_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # No thumbnail caching: an earlier render already queued these rows.
    return templates.TemplateResponse(request, "_fragment_home.html", home_context(db, user.id))


@router.get("/queue", response_class=HTMLResponse)
def queue_fragment(
    request: Request,
    ids: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The player's Queue panel; the queue lives in the browser, so ids come on the query string.

    Unparsable ids are dropped rather than rejected, so a stale sessionStorage list costs a row, not a 422.
    """
    parsed = [int(part) for part in ids.split(",") if part.strip().lstrip("-").isdigit()]
    return templates.TemplateResponse(
        request, "_fragment_queue.html", queue_panel_context(db, user.id, parsed)
    )


@router.get("/library", response_class=HTMLResponse)
def library_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(request, "_fragment_library.html", library_context(db, user.id))


@router.get("/storage-summary", response_class=HTMLResponse)
def storage_summary_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
        return templates.TemplateResponse(
        request, "_fragment_storage_summary.html", storage_summary_context(db, user.id)
    )


@router.get("/about", response_class=HTMLResponse)
def about_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Settings' version row, so an update found after load shows without a reload."""
    return templates.TemplateResponse(request, "_fragment_about.html", about_context(db, user.id))


@router.get("/detail/playlist/{kind}", response_class=HTMLResponse)
def playlist_detail_fragment(
    kind: str,
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = playlist_detail_context(db, user.id, kind, page)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playlist")
    # "New releases" holds releases, which have no Content rows to cache thumbnails for.
    if "content" in context:
        queue_thumbnail_caching(background_tasks, context["content"])
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/user-playlist/{playlist_id}", response_class=HTMLResponse)
def user_playlist_detail_fragment(
    playlist_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = user_playlist_detail_context(db, user.id, playlist_id, page)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such playlist")
    queue_thumbnail_caching(background_tasks, context["content"])
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


# The routes below render live from YouTube, not the database: slow, and no Content rows to cache for.


@router.get("/detail/yt-playlist/{playlist_id}", response_class=HTMLResponse)
def remote_playlist_fragment(playlist_id: str, request: Request) -> HTMLResponse:
    # Validated before it's interpolated into a youtube.com URL.
    if not PLAYLIST_ID_RE.match(playlist_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    context = remote_playlist_context(playlist_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this playlist")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/yt-artist/{browse_id}", response_class=HTMLResponse)
def remote_artist_fragment(
    browse_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """An artist's YouTube Music page; falls back to a channel context when the id isn't an artist."""
    if not CHANNEL_ID_RE.match(browse_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    context = remote_artist_context(db, user.id, browse_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this artist")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/yt-artist-songs/{browse_id}", response_class=HTMLResponse)
def remote_artist_songs_fragment(
    browse_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """An artist's full song list; its own route so back navigation returns to the profile."""
    if not CHANNEL_ID_RE.match(browse_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    context = remote_artist_songs_context(db, user.id, browse_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this artist")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


def _single_track_payload(track) -> dict:
    """A one-track release, keyed like _remote_track_row.html's dataset (all strings) for playRemoteVideo."""
    return {
        "videoId": track.video_id,
        "title": track.title,
        "channelId": track.channel_id or "",
        "thumbnailUrl": track.thumbnail_url or "",
        "durationSeconds": str(track.duration_seconds) if track.duration_seconds else "",
        "channelTitle": track.channel_title or "",
    }


@router.get("/detail/yt-release/{browse_id}", response_class=HTMLResponse)
def remote_release_fragment(browse_id: str, request: Request) -> Response:
    """An album or single: HTML panel for several tracks, JSON for exactly one so it plays directly.

    Decided by track count, not YouTube's "Single" type, which also labels multi-track releases.
    """
    if not RELEASE_ID_RE.match(browse_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")

    context = remote_release_context(browse_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this release")

    tracks = context["content"]
    if len(tracks) == 1:
        return JSONResponse(_single_track_payload(tracks[0]))
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/yt-mood/{slug}", response_class=HTMLResponse)
def remote_mood_fragment(slug: str, request: Request) -> HTMLResponse:
    """A mood's playlists, by the URL name mood_slug() gave it."""
    if len(slug) > MOOD_SLUG_MAX_LENGTH or not MOOD_SLUG_RE.match(slug):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    context = remote_mood_context(slug)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)
