"""What the client asks on open and on every return to the foreground: is anything out of date?

Two different staleness questions, one answer, because they have the same trigger:

- `version` is what the server is running. The client compares it with the version baked into
  the document it loaded; a difference means this page is older than the server that served it,
  which is routine for an installed PWA that goes days without reloading.
- `latest` is what the project has released since, from services/update_check.py. Only the
  account that set the instance up is told — see is_instance_owner.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_current_user, get_db, require_login
from app.models import User
from app.schemas import UpdateCheckSettingsIn, UpdateStatus
from app.services.update_check import (
    available_update,
    check_if_due,
    is_instance_owner,
    set_enabled,
)
from app.version import APP_VERSION, RELEASES_URL

router = APIRouter(prefix="/updates", tags=["updates"], dependencies=[Depends(require_login)])


def _status(db: Session, user_id: int) -> UpdateStatus:
    update = available_update(db) if is_instance_owner(db, user_id) else None
    return UpdateStatus(
        version=APP_VERSION,
        latest=update[0] if update else None,
        release_url=update[1] if update else RELEASES_URL,
    )


@router.get("", response_model=UpdateStatus)
def update_status(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UpdateStatus:
    """Answers from the stored row; the upstream call, if one is due at all, happens after the response."""
    background_tasks.add_task(check_if_due)
    return _status(db, user.id)


@router.put("/settings", response_model=UpdateStatus)
def update_settings(
    payload: UpdateCheckSettingsIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UpdateStatus:
    """Settings' toggle. Not the check itself — flips whether check_if_due is ever allowed to run.

    404 with the setting off, matching GET /updates — an env-disabled install has no toggle to
    flip. Owner-only — the toggle is never rendered for anyone else, and their flipping it would
    change what the owner sees without the owner having touched anything.
    """
    if not settings.update_check:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not is_instance_owner(db, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN)

    set_enabled(db, payload.enabled)
    return _status(db, user.id)
