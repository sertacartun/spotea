from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests
from app.models import User
from app.page_context import (
    downloads_context,
    home_context,
    home_shelf_items,
    library_context,
    queue_thumbnail_caching,
)
from app.services.refresh import queue_due_refresh
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app — Home, Library, Explore and Settings are
    tab panels in one document (see its inline head script), so this single
    route builds the context for all of them at once.

    Each region's context comes from app/page_context.py, shared with the
    fragment endpoints (routers/partials.py) that re-render that same region
    later. The full page and a refresh of one part of it therefore can't
    disagree about what it contains."""
    home = home_context(db, user.id)
    queue_thumbnail_caching(background_tasks, home_shelf_items(home))
    # Opening the app is what triggers a look for new releases now, rather
    # than a background loop running whether or not anyone is here (see
    # services/refresh.py). Queued behind this response, so this render still
    # shows what was already stored and the next one shows what arrived.
    queue_due_refresh(background_tasks, user)
    interests = parse_interests(user.interests)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "audio_quality": user.audio_quality,
            # Labels the Settings panel — the login is otherwise never shown
            # anywhere in the app after registration.
            "account_name": user.username,
            # Server-rendered rather than fetched by home/settings.js on boot:
            # the interest chips are part of the Settings panel's first paint,
            # and filling them in afterwards flashes an empty editor on every
            # load. Explore's recommendations are the opposite case — they can
            # cost a YouTube round trip, so they stay a deliberate fetch.
            "interests": interests,
            **home,
            **library_context(db, user.id),
            **downloads_context(db, user.id),
        },
    )


# Channel, the pinned playlists, and the player all moved in-page (see
# home/detail.js, home/overlay.js) — index.html's hash router handles them
# now. These redirects exist only so a link or bookmark from before that
# change still lands somewhere real.
@router.get("/favorites")
def favorites_redirect() -> RedirectResponse:
    return RedirectResponse("/#favorites")


@router.get("/new-uploads")
def new_uploads_redirect() -> RedirectResponse:
    return RedirectResponse("/#new-uploads")


@router.get("/recently-played")
def recently_played_redirect() -> RedirectResponse:
    return RedirectResponse("/#recently-played")


@router.get("/player/{content_id}")
def player_redirect(content_id: int) -> RedirectResponse:
    return RedirectResponse(f"/#player/{content_id}")
