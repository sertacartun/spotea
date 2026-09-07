from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests, serialize_interests
from app.models import User
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_login)])

AUDIO_QUALITIES = ("high", "low")


def _settings_out(user: User) -> SettingsOut:
    """Both endpoints answer with the same full settings shape — a PUT that
    replied with only the fields it changed would leave the client guessing
    what the rest ended up as (interests in particular, which the server
    normalizes on the way in)."""
    return SettingsOut(
        audio_quality=user.audio_quality,
        interests=parse_interests(user.interests),
    )


@router.get("", response_model=SettingsOut)
def get_settings(user: User = Depends(get_current_user)) -> SettingsOut:
    return _settings_out(user)


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SettingsOut:
    if payload.audio_quality is not None:
        if payload.audio_quality not in AUDIO_QUALITIES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio quality")
        user.audio_quality = payload.audio_quality

    if payload.interests is not None:
        # Normalized rather than validated: an interest is free text, so
        # there's nothing to reject — over-long or duplicate tags are
        # something to clean up, not a 400. The client is told what actually
        # got stored by the SettingsOut it gets back.
        user.interests = serialize_interests(payload.interests)
        # The recommendation cache isn't cleared here — it's keyed by an
        # interests hash and goes stale on its own the moment this list
        # changes (see services/recommendations.py). Leaving the row alone
        # means editing interests back to a previous set still hits its
        # cached batch instead of paying for a rebuild.

    db.commit()
    return _settings_out(user)
