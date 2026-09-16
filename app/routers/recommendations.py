"""Explore's interest-based "For you" shelves; GET serves the cache, POST /refresh forces a rebuild."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests
from app.models import User
from app.schemas import RecommendationsOut
from app.services.recommendations import get_recommendations

router = APIRouter(
    prefix="/recommendations", tags=["recommendations"], dependencies=[Depends(require_login)]
)


def _recommendations_out(db: Session, user: User, *, force: bool) -> RecommendationsOut:
    batch, generated_at = get_recommendations(db, user, force=force)
    return RecommendationsOut(
        interests=parse_interests(user.interests),
        generated_at=generated_at,
        **batch,
    )


@router.get("", response_model=RecommendationsOut)
def read_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RecommendationsOut:
    """The current batch, from cache when available; only builds (and hits YouTube) when there isn't one."""
    return _recommendations_out(db, user, force=False)


@router.post("/refresh", response_model=RecommendationsOut)
def refresh_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RecommendationsOut:
    """Always rebuild, resampling interests; called by the app-wide "Refresh artists" button."""
    return _recommendations_out(db, user, force=True)
