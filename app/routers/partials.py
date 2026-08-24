"""Re-rendering one region of index.html on demand.

index.html is rendered once, server-side, and nothing revalidates it. Every
action that changes what it shows — saving, favoriting, playing, finishing a
download, clearing storage — therefore had to be reflected into the DOM by
hand, and the app had accumulated six separate hand-written patchers doing
exactly that. Each new surface needed another one, and each was free to miss
a case; a shelf that only updated on some of the paths that should have
touched it looked identical to one that worked.

These endpoints return the same markup index.html renders for that region,
from the same context functions (see app/page_context.py). The client swaps
it in wholesale. That trades a small request for not having to know, per
action, which parts of the page it invalidated — and it picks up changes
this tab didn't make at all (the background refresh, another device, another
tab) for free.

Each response is one or more <template data-target="…"> blocks, so a single
render can update several places at once.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.models import User
from app.page_context import (
    downloads_context,
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
from app.youtube.urls import CHANNEL_ID_RE, MOOD_PARAMS_RE, PLAYLIST_ID_RE, RELEASE_ID_RE

router = APIRouter(prefix="/partials", tags=["partials"], dependencies=[Depends(require_login)])


@router.get("/home", response_class=HTMLResponse)
def home_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # No thumbnail caching queued here, unlike the full page render: a
    # fragment refresh only ever shows rows some earlier render already
    # queued, so doing it again would be a second pass over the same videos.
    return templates.TemplateResponse(request, "_fragment_home.html", home_context(db, user.id))


@router.get("/queue", response_class=HTMLResponse)
def queue_fragment(
    request: Request,
    ids: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The player's "Queue" panel.

    Unlike every other fragment here, the thing being rendered isn't
    server-side state: the queue and its order live in the browser (see
    static/js/home/queue.js), so the ids come in on the query string and this
    only turns them into rows. Rendering it here anyway is what makes a queue
    row look exactly like a playlist row — same template, same artwork, same
    duration — instead of being a second, hand-built list that has to be kept
    looking like the first.

    Unparsable ids are dropped rather than rejected. The list is a client's
    own sessionStorage record, and a stale or half-written one should cost
    the panel a row, not a 422 that leaves it empty.
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


@router.get("/downloads", response_class=HTMLResponse)
def downloads_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # The Downloads modal's own full list — deliberately not part of the
    # default refreshFragments() sweep, see fragments.js's
    # refreshDownloadsBody. Fetched only when the modal is actually opened
    # or an action inside it changes what it shows.
    return templates.TemplateResponse(
        request, "_fragment_downloads.html", downloads_context(db, user.id)
    )


@router.get("/storage-summary", response_class=HTMLResponse)
def storage_summary_fragment(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # The cheap half of what downloads_fragment above used to return in one
    # response — just the Settings "Storage used" line, via
    # storage.usage_summary rather than collect_usage's per-row work. This is
    # what refreshFragments() actually calls after every save/favorite/play.
    return templates.TemplateResponse(
        request, "_fragment_storage_summary.html", storage_summary_context(db, user.id)
    )


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
    # "New releases" is the one kind here that isn't a track list — it holds
    # releases, which have no Content row and so no thumbnail of ours to
    # cache (their covers are proxied straight from YouTube Music).
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
    """One hand-made playlist, through the same panel the pinned ones use.

    Separate from playlist_detail_fragment above because it is addressed by
    id rather than by kind — the pinned three are a fixed vocabulary, these
    are rows — but it renders the identical template with the identical
    context shape (see page_context.user_playlist_detail_context).
    """
    context = user_playlist_detail_context(db, user.id, playlist_id, page)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such playlist")
    queue_thumbnail_caching(background_tasks, context["content"])
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


# The three below render the same panel from YouTube rather than the database —
# Explore's search results and recommendations drill into them (see
# services/remote_detail.py). No queue_thumbnail_caching: that works on
# Content rows, and nothing here has one yet. All three are slow (a live
# read) where the three above are not, which is why the client shows its
# loading state for them the same way.


@router.get("/detail/yt-playlist/{playlist_id}", response_class=HTMLResponse)
def remote_playlist_fragment(playlist_id: str, request: Request) -> HTMLResponse:
    # Validated before it's interpolated into a youtube.com URL — it arrives
    # as a raw path parameter.
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
    """An artist's YouTube Music page. Same id shape as the channel route
    above — an artist's browse id *is* a channel id — and it falls back to
    that route's context when the id turns out not to name an artist (see
    remote_artist_context), which is also why it takes the same untrusted
    `avatar` hint: a channel card clicks through to *this* route now, and
    the hint is what keeps the hero from rendering blank on the fallback."""
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
    """Everything an artist has, as one list — the profile's "See all".
    Its own address rather than a query flag on the route above, because
    it's a separate view in history: pressing back from here goes to the
    profile, which is where the person came from."""
    if not CHANNEL_ID_RE.match(browse_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    context = remote_artist_songs_context(db, user.id, browse_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this artist")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


def _single_track_payload(track) -> dict:
    """The one track of a one-track release, keyed exactly the way
    _remote_track_row.html writes its dataset — so home/remote.js's
    playRemoteVideo takes this without caring whether it came off an element
    or off the wire. Strings throughout for the same reason: a dataset read
    only ever yields strings, and that function already normalizes them.
    """
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
    """An album or single, opened from an artist's profile. One route for
    both — YouTube Music answers them identically (see music.fetch_release).

    Two response shapes, though, and which one you get is the whole of "a
    single plays instead of opening":

    - More than one track: the panel, as HTML, like every other detail route.
    - Exactly one: that track as JSON, and no panel at all. A one-track
      release's panel was a cover, a title and a single row — a page whose
      only content was a button to do the thing you had already asked for.

    "Exactly one track" rather than YouTube Music's own "Single" type,
    because the type is not the question being asked: YT labels plenty of
    two- and three-track releases "Single" too, and force-playing the first
    of those would make the rest unreachable. It also costs nothing to be
    accurate here — the track count and the video id arrive in the same
    fetch, so this branch is free either way.

    The client (home/detail.js's resolveRelease) tells the two apart by
    content type and caches whichever it got, so a second click on the same
    release costs no YouTube request regardless of which shape it is.
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


@router.get("/detail/yt-mood/{params}", response_class=HTMLResponse)
def remote_mood_fragment(params: str, request: Request, title: str | None = None) -> HTMLResponse:
    """A mood's playlists, opened from Explore's "Moods & genres" row.

    `title` arrives as a query param rather than being looked up here:
    get_mood_playlists' own response carries no header naming its category
    (see remote_detail.remote_mood_context), and the normal open-from-Explore
    click already has it on hand from the list Explore just rendered — only
    a reload or a shared link arrives without it, and remote_mood_context
    falls back to one extra request for exactly that case."""
    if not MOOD_PARAMS_RE.match(params):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    context = remote_mood_context(params, title)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)
