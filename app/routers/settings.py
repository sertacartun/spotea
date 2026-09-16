from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests, serialize_interests
from app.models import User
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_login)])

AUDIO_QUALITIES = ("high", "low")


def _settings_out(user: User) -> SettingsOut:
    """Full settings shape from both endpoints, since interests are normalized on the way in."""
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
        user.interests = serialize_interests(payload.interests)
        # Recommendation cache is keyed by an interests hash, so it isn't cleared here.

    db.commit()
    return _settings_out(user)
