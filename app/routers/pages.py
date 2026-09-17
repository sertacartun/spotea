from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests
from app.models import User
from app.page_context import (
    home_context,
    home_shelf_items,
    library_context,
    queue_thumbnail_caching,
    storage_summary_context,
)
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app: one render builds every tab panel's context."""
    home = home_context(db, user.id)
    queue_thumbnail_caching(background_tasks, home_shelf_items(home))
    interests = parse_interests(user.interests)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "audio_quality": user.audio_quality,
            "account_name": user.username,
            # Server-rendered so the Settings chips don't flash empty on load.
            "interests": interests,
            **home,
            **library_context(db, user.id),
            **storage_summary_context(db, user.id, backfill=True),
        },
    )


# Legacy URLs, kept so old links still land.
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
