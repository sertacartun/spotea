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
from app.routes import LIBRARY_LIST_KINDS, SHELL_HEADER, SHELL_PATHS, detail_path
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


def app_shell(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app: one render builds every tab panel's context, whatever the path.

    The client routes off location.pathname, so every page URL returns this same document.
    """
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
        headers={SHELL_HEADER: "1"},
    )


for path in SHELL_PATHS:
    router.add_api_route(path, app_shell, methods=["GET"], response_class=HTMLResponse)


def _redirect_to(target: str):
    return lambda: RedirectResponse(target)


# Pre-/library URLs, kept so old links and bookmarks still land. Old #fragment links are rewritten client-side.
for kind in LIBRARY_LIST_KINDS:
    if kind != "downloads":
        router.add_api_route(f"/{kind}", _redirect_to(detail_path(kind)), methods=["GET"])
